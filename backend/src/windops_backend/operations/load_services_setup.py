"""Owned, finite migration/seed/bucket initialization for a fresh services run."""

from __future__ import annotations

import argparse
import asyncio
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from minio import Minio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from urllib3 import PoolManager, Timeout

from windops_backend.config import Settings
from windops_backend.operations.load_services_dataset import (
    seed_services_dataset,
    snapshot_services_dataset,
)
from windops_backend.operations.load_services_server import load_runtime
from windops_backend.operations.load_software import ROOT


def migration_config() -> Config:
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    return config


def create_buckets(settings: Settings) -> list[str]:
    """Fresh owned storage only; each native network operation has a hard timeout."""
    transport = PoolManager(timeout=Timeout(connect=3, read=5), retries=False)
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key.get_secret_value(),
        secure=settings.minio_secure,
        http_client=transport,
    )
    buckets = [
        settings.minio_field_evidence_bucket,
        settings.minio_knowledge_bucket,
        settings.minio_model_bucket,
        settings.minio_twin_bucket,
        settings.minio_care_bucket,
    ]
    try:
        if len(set(buckets)) != 5 or client.list_buckets():
            raise RuntimeError("SETUP_STORAGE_NOT_EMPTY")
        for bucket in buckets:
            client.make_bucket(bucket)
            if not client.bucket_exists(bucket):
                raise RuntimeError("SETUP_BUCKET_NOT_OBSERVED")
        return buckets
    finally:
        transport.clear()


async def initialize(settings: Settings, rows: int, expected_head: str) -> dict[str, Any]:
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 5, "command_timeout": 30},
    )
    try:
        async with engine.connect() as connection:
            heads = list(
                (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalars()
            )
        if heads != [expected_head] or engine.dialect.name != "postgresql":
            raise RuntimeError("SETUP_MIGRATION_NOT_OBSERVED")
        dataset = await seed_services_dataset(engine, rows)
        snapshot = await snapshot_services_dataset(engine)
        # This helper runs inside the owned command containment; cancellation or
        # a blocked native call is terminated with the whole process tree.
        buckets = await asyncio.to_thread(create_buckets, settings)
        return {
            "migration_head": expected_head,
            "dataset": dataset,
            "database_before": snapshot,
            "buckets": buckets,
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--history-rows", type=int, required=True)
    args = parser.parse_args()
    try:
        if not 90 <= args.history_rows <= 1_000_000:
            raise ValueError("invalid synthetic row count")
        settings = load_runtime(args.run_directory, args.run_id, "api", args.runtime_sha256)
        # Alembic's existing environment imports this getter. Only this isolated
        # process is configured; no dotenv or provider configuration is loaded.
        from windops_backend import config as configuration

        @lru_cache
        def isolated_settings() -> Settings:
            return settings

        configuration.get_settings = isolated_settings
        config = migration_config()
        heads = ScriptDirectory.from_config(config).get_heads()
        if len(heads) != 1:
            raise RuntimeError("SETUP_MIGRATION_HEAD_AMBIGUOUS")
        command.upgrade(config, "head")
        evidence = asyncio.run(initialize(settings, args.history_rows, heads[0]))
        with (args.run_directory / "private" / "setup.json").open("x", encoding="utf-8") as stream:
            json.dump(
                {"run_id": args.run_id, "runtime_sha256": args.runtime_sha256, **evidence},
                stream,
                allow_nan=False,
            )
        return 0
    except Exception:
        print("SERVICES_SETUP_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
