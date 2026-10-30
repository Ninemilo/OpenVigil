"""Fresh, owned evaluation databases; never reuse or reset an existing database."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from windops_backend import storage as _storage  # noqa: F401
from windops_backend.models import Base

MIGRATION_PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_TIMEOUT_SECONDS = 120


def local_admin_url(value: str) -> URL:
    """An operator must explicitly supply local isolated PostgreSQL administration."""
    url = make_url(value)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "::1", "localhost"}
        or not url.username
        or not url.database
        or url.query
    ):
        raise ValueError("evaluation requires an explicit loopback PostgreSQL admin URL")
    return url


def _plain_directory(folder: Path) -> Path:
    # Check the original path before resolving, including Windows junction ancestors.
    absolute = folder.absolute()
    for candidate in (absolute, *absolute.parents):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & (
            stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("evaluation database directory must not traverse a link or junction")
    if not absolute.is_dir():
        raise ValueError("evaluation database folder must be an existing owned directory")
    return absolute.resolve()


@dataclass(frozen=True)
class _DatabaseIdentity:
    folder: Path
    name: str
    admin_url: URL | None


class OwnedEvaluationDatabase:
    def __init__(self, folder: Path, *, admin_url: str | None = None) -> None:
        self._identity = _DatabaseIdentity(
            folder=_plain_directory(folder),
            name="windops_eval_" + uuid4().hex,
            admin_url=local_admin_url(admin_url) if admin_url is not None else None,
        )
        self._engine: AsyncEngine | None = None
        self._admin: AsyncEngine | None = None
        self._owned_postgres: _DatabaseIdentity | None = None
        self._creation_uncertain = False
        self._created_once = False
        self._schema_initialized = False
        self._open_attempted = False
        self._closed = False
        self._cleanup_passed = False
        self._sqlite_identity: tuple[int, int] | None = None
        self._url = ""
        self._lifecycle = asyncio.Lock()

    @property
    def folder(self) -> Path:
        return self._identity.folder

    @property
    def name(self) -> str:
        return self._identity.name

    @property
    def admin_url(self) -> URL | None:
        return self._identity.admin_url

    @property
    def engine(self) -> AsyncEngine | None:
        return self._engine

    @property
    def url(self) -> str:
        return self._url

    @property
    def cleanup_passed(self) -> bool:
        return self._cleanup_passed

    async def open(self) -> AsyncEngine:
        async with self._lifecycle:
            if self._open_attempted or self._closed:
                raise RuntimeError("evaluation database cannot be opened twice or after close")
            self._open_attempted = True
            try:
                _plain_directory(self.folder)
                if self.admin_url is None:
                    path = self.folder / "workflow.sqlite"
                    # Exclusive creation never overwrites an existing file, including a link.
                    with path.open("xb") as created:
                        info = os.fstat(created.fileno())
                        self._sqlite_identity = (info.st_dev, info.st_ino)
                    self._created_once = True
                    self._url = URL.create(
                        "sqlite+aiosqlite", database=str(path)
                    ).render_as_string()
                    self._engine = create_async_engine(self.url)
                    async with self._engine.begin() as connection:
                        await connection.run_sync(Base.metadata.create_all)
                else:
                    identity = self._identity
                    self._admin = create_async_engine(self.admin_url, isolation_level="AUTOCOMMIT")
                    async with self._admin.connect() as connection:
                        self._creation_uncertain = True
                        await connection.execute(text(f'CREATE DATABASE "{identity.name}"'))
                        # Record ownership immediately after CREATE returns, before context exit.
                        self._creation_uncertain = False
                        self._owned_postgres = identity
                        self._created_once = True
                    self._url = self.admin_url.set(database=identity.name).render_as_string(
                        hide_password=False
                    )
                    await self._migrate()
                    self._engine = create_async_engine(self.url)
                self._schema_initialized = True
                return self._engine
            except BaseException as error:
                try:
                    await self._close_resources()
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "evaluation database initialization and cleanup failed",
                        [error, cleanup_error],
                    ) from None
                raise

    async def _migrate(self) -> None:
        if self._owned_postgres is None or self._identity != self._owned_postgres:
            raise RuntimeError("migration requires exact newly created database ownership")
        expected = self._owned_postgres.admin_url
        if expected is None or make_url(self.url) != expected.set(
            database=self._owned_postgres.name
        ):
            raise RuntimeError("refusing migration against a different database")
        backend = MIGRATION_PROJECT_ROOT
        if not (backend / "alembic.ini").is_file():
            raise RuntimeError("evaluation requires the source checkout migration project")
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("WINDOPS_", "PG"))
        }
        env.update(
            WINDOPS_DATABASE_URL=self.url,
            WINDOPS_ENVIRONMENT="development",
            WINDOPS_SCHEMA_BOOTSTRAP="false",
            WINDOPS_DEMO_SEED="false",
            PYTHONPATH=str(backend / "src"),
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
            cwd=backend,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(MIGRATION_TIMEOUT_SECONDS):
                await process.wait()
            if process.returncode != 0:
                raise RuntimeError("evaluation database migration failed")
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # It exited between the returncode check and the kill request.
                finally:
                    await process.wait()

    async def close(self) -> None:
        async with self._lifecycle:
            await self._close_resources()

    def _retained_sqlite(self) -> bool:
        if self._sqlite_identity is None:
            return False
        try:
            _plain_directory(self.folder)
            info = (self.folder / "workflow.sqlite").lstat()
            return (
                stat.S_ISREG(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and (info.st_dev, info.st_ino) == self._sqlite_identity
            )
        except (OSError, ValueError):
            return False

    async def _close_resources(self) -> None:
        self._closed = True
        self._cleanup_passed = False
        errors: list[BaseException] = []
        if self._engine is not None:
            try:
                await self._engine.dispose()
                self._engine = None
            except BaseException as exc:
                errors.append(exc)
        if self._owned_postgres is not None:
            try:
                owner = self._owned_postgres
                if (
                    self._admin is None
                    or owner != self._identity
                    or re.fullmatch(r"windops_eval_[a-f0-9]{32}", owner.name) is None
                ):
                    raise RuntimeError("refusing cleanup without exact owned database identity")
                async with self._admin.connect() as connection:
                    # No FORCE or connection termination: unexpected users make cleanup fail.
                    await connection.execute(text(f'DROP DATABASE "{owner.name}"'))
                    self._owned_postgres = None
            except BaseException as exc:
                errors.append(exc)
        if self._admin is not None:
            try:
                await self._admin.dispose()
                if self._owned_postgres is None:
                    self._admin = None
            except BaseException as exc:
                errors.append(exc)
        if self._sqlite_identity is not None and not self._retained_sqlite():
            errors.append(RuntimeError("owned SQLite evidence is missing or replaced"))
        if self._creation_uncertain:
            # A lost acknowledgement is not proof that no database was created. Do not
            # guess ownership or delete; retain the generated identity for investigation.
            errors.append(RuntimeError("PostgreSQL creation outcome is unconfirmed"))
        if errors:
            raise BaseExceptionGroup("evaluation database cleanup failed", errors)
        self._cleanup_passed = self._open_attempted

    def evidence(self) -> dict[str, Any]:
        return {
            "backend": "postgresql" if self.admin_url is not None else "sqlite",
            "identity": self.name if self.admin_url is not None else "workflow.sqlite",
            "fresh_database": self._created_once,
            "creation_uncertain": self._creation_uncertain,
            "schema": (
                ("alembic" if self.admin_url is not None else "test_metadata")
                if self._schema_initialized
                else None
            ),
            "cleanup_passed": self.cleanup_passed,
            "sqlite_evidence_retained": self.admin_url is None and self._retained_sqlite(),
        }
