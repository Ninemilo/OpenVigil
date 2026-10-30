"""Real SQLite ownership checks; PostgreSQL boundaries injected, no PostgreSQL network calls."""

import asyncio
import hashlib
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from windops_backend.operations import workflow_eval_database as database_module
from windops_backend.operations.workflow_eval_database import (
    OwnedEvaluationDatabase,
    local_admin_url,
)

ADMIN_URL = "postgresql+asyncpg://synthetic:fixture-only@127.0.0.1:5432/postgres"
ACTUAL_MIGRATE = OwnedEvaluationDatabase._migrate


class BoundaryEngine:
    def __init__(self, statements):
        self.statements = statements
        self.dispose_calls = 0
        self.dispose_failure = False
        self.create_failure = False
        self.drop_failure = False

    @asynccontextmanager
    async def connect(self):
        yield self

    async def execute(self, statement):
        value = str(statement)
        self.statements.append(value)
        if value.startswith("CREATE") and self.create_failure:
            raise RuntimeError("synthetic create failure")
        if value.startswith("DROP") and self.drop_failure:
            raise RuntimeError("synthetic drop failure")

    async def dispose(self):
        self.dispose_calls += 1
        if self.dispose_failure:
            raise RuntimeError("synthetic dispose failure")


@pytest.fixture
def pg_boundary(monkeypatch):
    statements = []
    admin = BoundaryEngine(statements)
    target = BoundaryEngine(statements)
    urls = []

    def engine(url, **options):
        urls.append(make_url(url))
        return admin if options.get("isolation_level") == "AUTOCOMMIT" else target

    async def migrate(self):
        assert make_url(self.url).database == self.name

    monkeypatch.setattr(database_module, "create_async_engine", engine)
    monkeypatch.setattr(OwnedEvaluationDatabase, "_migrate", migrate)
    return admin, target, statements, urls


async def test_existing_sqlite_is_never_opened_replaced_or_claimed(tmp_path):
    path = tmp_path / "workflow.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE protected_marker (value TEXT)")
        connection.execute("INSERT INTO protected_marker VALUES ('synthetic-existing')")
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    database = OwnedEvaluationDatabase(tmp_path)
    with pytest.raises(FileExistsError):
        await database.open()
    await database.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    assert database.evidence()["fresh_database"] is False
    assert database.evidence()["sqlite_evidence_retained"] is False


async def test_postgres_identity_cannot_be_retargeted_to_another_legal_database(
    tmp_path, pg_boundary
):
    _, _, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    owned_name = database.name
    await database.open()
    with pytest.raises(AttributeError):
        database.name = "windops_eval_" + "a" * 32
    await database.close()
    assert statements == [f'CREATE DATABASE "{owned_name}"', f'DROP DATABASE "{owned_name}"']


async def test_migration_failure_automatically_cleans_only_created_database(
    tmp_path, pg_boundary, monkeypatch
):
    admin, _, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)

    async def fail(self):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(OwnedEvaluationDatabase, "_migrate", fail)
    with pytest.raises(RuntimeError, match="migration failure"):
        await database.open()
    assert statements == [f'CREATE DATABASE "{database.name}"', f'DROP DATABASE "{database.name}"']
    assert admin.dispose_calls == 1
    assert database.evidence()["cleanup_passed"] is True
    assert database.evidence()["schema"] is None


async def test_admin_dispose_failure_cannot_report_cleanup_passed(tmp_path, pg_boundary):
    admin, _, _, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    await database.open()
    admin.dispose_failure = True
    with pytest.raises(ExceptionGroup):
        await database.close()
    assert database.evidence()["cleanup_passed"] is False


async def test_real_sqlite_schema_close_and_evidence_retention(tmp_path):
    database = OwnedEvaluationDatabase(tmp_path)
    initial = database.evidence()
    assert initial["fresh_database"] is False
    assert initial["schema"] is None
    assert initial["cleanup_passed"] is False
    assert initial["sqlite_evidence_retained"] is False
    engine = await database.open()
    async with engine.begin() as connection:
        names = (
            (await connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'")))
            .scalars()
            .all()
        )
        assert {"missions", "evidence", "agent_executions", "outbox_events"} <= set(names)
        await connection.execute(text("CREATE TABLE evaluation_marker (value TEXT)"))
        await connection.execute(text("INSERT INTO evaluation_marker VALUES ('owned-run')"))
    assert database.evidence()["schema"] == "test_metadata"
    assert database.evidence()["cleanup_passed"] is False
    await database.close()
    await database.close()  # Idempotent close never deletes retained SQLite evidence.
    result = database.evidence()
    assert result["fresh_database"] is True
    assert result["cleanup_passed"] is True
    assert result["sqlite_evidence_retained"] is True
    with sqlite3.connect(tmp_path / "workflow.sqlite") as connection:
        assert connection.execute("SELECT value FROM evaluation_marker").fetchone() == (
            "owned-run",
        )
    with pytest.raises(RuntimeError, match="opened twice"):
        await database.open()


async def test_open_is_one_shot_even_when_initialization_fails(tmp_path, pg_boundary, monkeypatch):
    admin, _, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)

    async def fail(self):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(OwnedEvaluationDatabase, "_migrate", fail)
    with pytest.raises(RuntimeError, match="migration failure"):
        await database.open()
    previous = list(statements)
    with pytest.raises(RuntimeError, match="opened twice"):
        await database.open()
    await database.close()
    assert statements == previous
    assert admin.dispose_calls == 1


async def test_concurrent_opens_create_exactly_one_owned_database(tmp_path, pg_boundary):
    _, _, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    results = await asyncio.gather(database.open(), database.open(), return_exceptions=True)
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert statements == [f'CREATE DATABASE "{database.name}"']
    await database.close()
    assert statements[-1] == f'DROP DATABASE "{database.name}"'


async def test_close_before_open_cannot_produce_success_or_allow_later_open(tmp_path, pg_boundary):
    _, _, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    await database.close()
    assert database.evidence()["cleanup_passed"] is False
    with pytest.raises(RuntimeError):
        await database.open()
    assert statements == []


async def test_identity_is_frozen_and_changed_private_identity_refuses_drop(tmp_path, pg_boundary):
    _, _, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    await database.open()
    owned = database._identity
    with pytest.raises(FrozenInstanceError):
        owned.name = "windops_eval_" + "a" * 32
    database._identity = replace(owned, name="windops_eval_" + "a" * 32)
    with pytest.raises(ExceptionGroup):
        await database.close()
    assert statements == [f'CREATE DATABASE "{owned.name}"']
    assert database.cleanup_passed is False
    database._identity = owned
    await database.close()
    assert statements[-1] == f'DROP DATABASE "{owned.name}"'


async def test_drop_failure_is_not_forced_or_hidden_and_can_be_retried(tmp_path, pg_boundary):
    admin, target, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    await database.open()
    admin.drop_failure = True
    with pytest.raises(ExceptionGroup):
        await database.close()
    assert not database.cleanup_passed
    assert target.dispose_calls == 1 and admin.dispose_calls == 1
    assert all("FORCE" not in value and "terminate" not in value for value in statements)
    admin.drop_failure = False
    await database.close()
    assert database.cleanup_passed
    assert target.dispose_calls == 1
    assert statements[-1] == f'DROP DATABASE "{database.name}"'


async def test_target_dispose_failure_still_attempts_admin_cleanup_and_stays_failed(
    tmp_path, pg_boundary
):
    admin, target, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    await database.open()
    target.dispose_failure = True
    with pytest.raises(ExceptionGroup):
        await database.close()
    assert admin.dispose_calls == 1
    assert statements[-1] == f'DROP DATABASE "{database.name}"'
    assert not database.cleanup_passed


async def test_unconfirmed_create_never_drops_a_database_or_claims_clean_success(
    tmp_path, pg_boundary
):
    admin, _, statements, _ = pg_boundary
    admin.create_failure = True
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    with pytest.raises(ExceptionGroup):
        await database.open()
    assert statements == [f'CREATE DATABASE "{database.name}"']
    assert admin.dispose_calls == 1
    assert database.evidence()["creation_uncertain"] is True
    assert database.evidence()["fresh_database"] is False
    assert database.evidence()["cleanup_passed"] is False


async def test_migration_and_cleanup_failures_are_both_preserved(
    tmp_path, pg_boundary, monkeypatch
):
    admin, _, _, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    admin.drop_failure = True

    async def fail(self):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(OwnedEvaluationDatabase, "_migrate", fail)
    with pytest.raises(ExceptionGroup) as error:
        await database.open()
    assert "migration failure" in str(error.value.exceptions[0])
    assert isinstance(error.value.exceptions[1], ExceptionGroup)
    assert not database.cleanup_passed


async def test_sqlite_engine_creation_failure_leaves_owned_empty_evidence(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic engine creation failure")

    monkeypatch.setattr(database_module, "create_async_engine", fail)
    database = OwnedEvaluationDatabase(tmp_path)
    with pytest.raises(RuntimeError, match="engine creation failure"):
        await database.open()
    assert (tmp_path / "workflow.sqlite").read_bytes() == b""
    assert database.evidence()["fresh_database"] is True
    assert database.evidence()["schema"] is None
    assert database.evidence()["cleanup_passed"] is True


async def test_replaced_sqlite_evidence_is_detected_without_deleting_replacement(tmp_path):
    database = OwnedEvaluationDatabase(tmp_path)
    await database.open()
    await database.close()
    path = tmp_path / "workflow.sqlite"
    path.rename(tmp_path / "original.sqlite")
    path.write_bytes(b"synthetic replacement")
    with pytest.raises(ExceptionGroup):
        await database.close()
    assert path.read_bytes() == b"synthetic replacement"
    assert not database.evidence()["sqlite_evidence_retained"]
    assert not database.cleanup_passed


@pytest.mark.parametrize(
    "query", ["host=remote.invalid", "database=existing", "user=other", "port=6543", "ssl=disable"]
)
def test_admin_query_parameters_cannot_override_explicit_loopback_target(query):
    with pytest.raises(ValueError):
        local_admin_url(ADMIN_URL + "?" + query)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://synthetic@remote.invalid/postgres",
        "sqlite+aiosqlite:///existing.sqlite",
        "postgresql://synthetic@127.0.0.1/postgres",
        "postgresql+asyncpg://127.0.0.1/postgres",
        "postgresql+asyncpg://synthetic@127.0.0.1",
    ],
)
def test_admin_url_rejects_unscoped_or_remote_targets(url):
    with pytest.raises(ValueError):
        local_admin_url(url)


class MigrationProcess:
    def __init__(self, mode):
        self.mode = mode
        self.returncode = None
        self.wait_calls = 0
        self.kill_calls = 0
        self.started = asyncio.Event()

    async def wait(self):
        self.wait_calls += 1
        self.started.set()
        if self.mode == "success":
            self.returncode = 0
        elif self.mode == "failure":
            self.returncode = 1
        elif self.returncode is None:
            await asyncio.Event().wait()
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        if self.mode == "exit_race":
            raise ProcessLookupError("synthetic child already exited")


@pytest.mark.parametrize("mode", ["success", "failure", "timeout", "exit_race", "cancel"])
async def test_actual_migration_launch_binds_owned_target_and_reaps_on_failure(
    tmp_path, pg_boundary, monkeypatch, mode
):
    admin, _, statements, _ = pg_boundary
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    process = MigrationProcess(mode)
    launched = []

    async def launch(*args, **kwargs):
        launched.append((args, kwargs))
        return process

    monkeypatch.setattr(OwnedEvaluationDatabase, "_migrate", ACTUAL_MIGRATE)
    monkeypatch.setattr(database_module.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(
        database_module, "MIGRATION_TIMEOUT_SECONDS", 0.01 if mode != "cancel" else 10
    )
    monkeypatch.setenv("WINDOPS_DATABASE_URL", "synthetic-existing-must-not-use")
    monkeypatch.setenv("WINDOPS_LLM_API_KEY", "synthetic-only-not-a-secret")
    monkeypatch.setenv("PGDATABASE", "synthetic-existing-must-not-use")
    if mode == "success":
        await database.open()
        assert database.evidence()["schema"] == "alembic"
        await database.close()
    elif mode == "cancel":
        pending = asyncio.create_task(database.open())
        await process.started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        expected_error = RuntimeError if mode == "failure" else TimeoutError
        with pytest.raises(expected_error):
            await database.open()
    assert len(launched) == 1
    command, options = launched[0]
    assert command[1:] == ("-m", "alembic", "upgrade", "head")
    assert options["cwd"] == database_module.MIGRATION_PROJECT_ROOT
    target = make_url(options["env"]["WINDOPS_DATABASE_URL"])
    assert target.database == database.name
    assert target.host == "127.0.0.1"
    assert "WINDOPS_LLM_API_KEY" not in options["env"]
    assert "PGDATABASE" not in options["env"]
    assert options["env"]["WINDOPS_SCHEMA_BOOTSTRAP"] == "false"
    assert options["env"]["WINDOPS_DEMO_SEED"] == "false"
    assert options["stdout"] == asyncio.subprocess.DEVNULL
    assert options["stderr"] == asyncio.subprocess.DEVNULL
    if mode in {"timeout", "exit_race", "cancel"}:
        assert process.kill_calls == 1 and process.wait_calls == 2
    assert statements == [f'CREATE DATABASE "{database.name}"', f'DROP DATABASE "{database.name}"']
    assert admin.dispose_calls == 1
    assert database.cleanup_passed
    if mode != "success":
        assert database.evidence()["schema"] is None


async def test_migration_refuses_modified_url_even_for_another_legal_database(
    tmp_path, pg_boundary, monkeypatch
):
    database = OwnedEvaluationDatabase(tmp_path, admin_url=ADMIN_URL)
    await database.open()
    original = database._url
    database._url = ADMIN_URL
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not launch a migration against an existing database")

    monkeypatch.setattr(database_module.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="different database"):
        await ACTUAL_MIGRATE(database)
    assert calls == []
    database._url = original
    await database.close()


async def test_sqlite_schema_initialization_failure_releases_connection(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic schema initialization failure")

    monkeypatch.setattr(database_module.Base.metadata, "create_all", fail)
    database = OwnedEvaluationDatabase(tmp_path)
    with pytest.raises(RuntimeError, match="schema initialization failure"):
        await database.open()
    assert database.engine is None
    assert database.cleanup_passed
    assert database.evidence()["schema"] is None
    path = tmp_path / "workflow.sqlite"
    # Windows rejects this rename if this evaluation left a database connection open.
    renamed = tmp_path / "retained-after-failed-schema.sqlite"
    path.rename(renamed)
    assert renamed.is_file()


def test_directory_reparse_ancestor_is_rejected_before_resolving_target(tmp_path, monkeypatch):
    original = database_module.Path.lstat

    def injected(path):
        if path == tmp_path.parent:
            return SimpleNamespace(
                st_mode=database_module.stat.S_IFDIR,
                st_file_attributes=database_module.stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return original(path)

    monkeypatch.setattr(database_module.Path, "lstat", injected)
    with pytest.raises(ValueError, match="link or junction"):
        OwnedEvaluationDatabase(tmp_path)
