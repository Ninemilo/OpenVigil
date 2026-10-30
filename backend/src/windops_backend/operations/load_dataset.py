"""Synthetic, isolated SQLite inputs and independent HTTP response oracles.

These inputs exercise the actual application's read paths. They are neither field
data nor a substitute for PostgreSQL/TimescaleDB, Redis, MinIO or OCR measurements.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Column, MetaData, Table, insert, select
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.ext.asyncio import create_async_engine

from windops_backend import storage as _storage  # noqa: F401
from windops_backend.model_base import Base
from windops_backend.model_identity_assets import Tenant, Turbine, WindFarm
from windops_backend.model_ingest_governance import IngestReceipt, IngestSource
from windops_backend.model_telemetry_operations import ScadaSample

START = datetime(2026, 1, 1, tzinfo=UTC)
VARIABLES = (("active_power", "MW"), ("wind_speed", "m/s"), ("rotor_speed", "rpm"))
USER_SCOPES = {"a1": "a", "a2": "a", "b1": "b", "b2": "b"}
SUBJECTS = {
    "a1": "operations-approver",
    "a2": "maintenance-reviewer",
    "b1": "operations-manager",
    "b2": "field-technician",
}
KEY_FIELDS = {
    "a1": "operations_approver_api_key",
    "a2": "maintenance_reviewer_api_key",
    "b1": "operations_manager_api_key",
    "b2": "field_technician_api_key",
}


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def sample_row(index: int) -> dict[str, Any]:
    scope_index, position = index % 3, index // 3
    scope = "abc"[scope_index]
    tick, variable_index = divmod(position, len(VARIABLES))
    variable, unit = VARIABLES[variable_index]
    return {
        "id": f"sample-{index:07d}",
        "source_event_id": f"receipt-{index:07d}",
        "source_id": f"source-{scope}",
        "source_sequence": position,
        "turbine_id": f"turbine-{scope}",
        "observed_at": START + timedelta(minutes=tick),
        "received_at": START + timedelta(minutes=tick, seconds=1),
        "is_late": tick % 17 == 0,
        "variable": variable,
        "unit": unit,
        "value": 1.0 + (scope_index * 100 + variable_index * 10 + tick % 10) / 10,
        "quality": "uncertain" if tick % 19 == 0 else "good",
        "attributes": {"classification": "synthetic_load_v1"},
    }


async def seed_dataset(database: Path, rows: int) -> dict[str, Any]:
    """Create a new file exclusively, then bootstrap only the test schema."""
    if not 90 <= rows <= 1_000_000:
        raise ValueError("software history requires 90..1000000 total rows")
    with database.open("xb"):
        pass
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    source_digest = hashlib.sha256()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.run_sync(Base.metadata.create_all)
            for scope in "abc":
                await connection.execute(
                    insert(Tenant), [{"id": f"tenant-{scope}", "name": f"Synthetic {scope}"}]
                )
                await connection.execute(
                    insert(WindFarm),
                    [
                        {
                            "id": f"farm-{scope}",
                            "tenant_id": f"tenant-{scope}",
                            "name": f"Synthetic farm {scope}",
                            "capacity_mw": 10.0,
                        }
                    ],
                )
                await connection.execute(
                    insert(Turbine),
                    [
                        {
                            "id": f"turbine-{scope}{suffix}",
                            "wind_farm_id": f"farm-{scope}",
                            "model": "synthetic-load-v1",
                        }
                        for suffix in ("", "-extra")
                    ],
                )
                await connection.execute(
                    insert(IngestSource),
                    [
                        {
                            "id": f"source-{scope}",
                            "display_name": f"Synthetic {scope}",
                            "source_kind": "rest",
                            "policy": {},
                        }
                    ],
                )
            for offset in range(0, rows, 1000):
                batch = [sample_row(i) for i in range(offset, min(rows, offset + 1000))]
                receipts = []
                for row in batch:
                    digest = canonical_hash(
                        {
                            key: value.isoformat() if isinstance(value, datetime) else value
                            for key, value in row.items()
                        }
                    )
                    source_digest.update(digest.encode("ascii"))
                    receipts.append(
                        {
                            "source_event_id": row["source_event_id"],
                            "source_id": row["source_id"],
                            "payload_hash": digest,
                            "received_at": row["received_at"],
                        }
                    )
                await connection.execute(insert(IngestReceipt), receipts)
                await connection.execute(insert(ScadaSample), batch)
            violations = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if violations:
                raise RuntimeError("synthetic dataset contains invalid references")
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            actual = connection.execute("SELECT COUNT(*) FROM scada_samples").fetchone()[0]
        if actual != rows:
            raise RuntimeError("synthetic dataset count mismatch")
        return {
            "generator": "openvigil.synthetic-load.v1",
            "classification": "synthetic",
            "history_rows": actual,
            "tenants": 3,
            "turbines": 6,
            "sources": 3,
            "variables": 3,
            "input_sha256": source_digest.hexdigest(),
            "window": history_query(rows, "a"),
            "foreign_keys_verified": True,
        }
    finally:
        await engine.dispose()


def history_query(rows: int, scope: str) -> dict[str, str]:
    end = START + timedelta(minutes=(rows - 1) // 9 + 1)
    return {
        "turbine_id": f"turbine-{scope}",
        "from": START.isoformat(),
        "to": end.isoformat(),
        "points_per_series": "100",
    }


def validate_turbines(value: object, user: str) -> bool:
    if not isinstance(value, dict) or user not in USER_SCOPES:
        return False
    scope = USER_SCOPES[user]
    rows = value.get("turbines")
    return (
        value.get("count") == 2
        and value.get("next_cursor") is None
        and isinstance(rows, list)
        and len(rows) == 2
        and [row.get("turbine_id") for row in rows if isinstance(row, dict)]
        == [f"turbine-{scope}", f"turbine-{scope}-extra"]
        and all(
            isinstance(row, dict) and row.get("wind_farm_id") == f"farm-{scope}" for row in rows
        )
    )


class HistoryOracle:
    """Expected complete aggregates derived from generated input, not HTTP output."""

    def __init__(self, rows: int) -> None:
        self.queries = {user: history_query(rows, scope) for user, scope in USER_SCOPES.items()}
        self.interval = math.ceil((((rows - 1) // 9 + 1) * 60) / 100)
        self.expected: dict[str, dict[str, dict[int, dict[str, Any]]]] = {}
        for index in range(rows):
            row = sample_row(index)
            scope = str(row["turbine_id"])[-1]
            if scope == "c":
                continue
            seconds = int((row["observed_at"] - START).total_seconds())
            buckets = self.expected.setdefault(scope, {}).setdefault(row["variable"], {})
            bucket = buckets.setdefault(
                seconds // self.interval,
                {"count": 0, "sum": 0.0, "late": False, "uncertain": False},
            )
            bucket["count"] += 1
            bucket["sum"] += row["value"]
            bucket["late"] = bucket["late"] or row["is_late"]
            bucket["uncertain"] = bucket["uncertain"] or row["quality"] == "uncertain"

    def validate(self, value: object, user: str) -> bool:
        if not isinstance(value, dict) or user not in USER_SCOPES:
            return False
        scope = USER_SCOPES[user]
        series, meta = value.get("data"), value.get("meta")
        expected = self.expected[scope]
        if not isinstance(series, list) or not isinstance(meta, dict) or len(series) != 3:
            return False
        if (
            meta.get("count") != 3
            or meta.get("interval_seconds") != self.interval
            or meta.get("turbine_id") != f"turbine-{scope}"
            or meta.get("point_count") != sum(len(items) for items in expected.values())
        ):
            return False
        seen: set[str] = set()
        for item in series:
            if not isinstance(item, dict):
                return False
            variable = item.get("variable")
            if not isinstance(variable, str) or variable not in expected or variable in seen:
                return False
            seen.add(variable)
            if (
                item.get("turbine_id") != f"turbine-{scope}"
                or item.get("source_id") != f"source-{scope}"
                or item.get("unit") != dict(VARIABLES)[variable]
            ):
                return False
            points = item.get("points")
            buckets = expected[variable]
            if not isinstance(points, list) or len(points) != len(buckets):
                return False
            for point, (bucket, raw) in zip(points, sorted(buckets.items()), strict=True):
                if not isinstance(point, dict):
                    return False
                actual = point.get("value")
                timestamp = point.get("timestamp")
                if not isinstance(actual, (int, float)) or isinstance(actual, bool):
                    return False
                if not isinstance(timestamp, str):
                    return False
                try:
                    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    return False
                if (
                    moment != START + timedelta(seconds=bucket * self.interval)
                    or not math.isclose(
                        actual, raw["sum"] / raw["count"], rel_tol=1e-10, abs_tol=1e-10
                    )
                    or point.get("late") is not raw["late"]
                    or point.get("quality") != ("uncertain" if raw["uncertain"] else "good")
                ):
                    return False
        return seen == set(expected)


def database_snapshot(database: Path) -> dict[str, Any]:
    """Persist only counts/hashes; audit rows are the sole expected read mutation."""
    tables: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for name in sorted(names):
            if name == "read_access_audits":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            # The SQLAlchemy compiler quotes schema identifiers; never interpolate
            # table/column names into executable SELECT text.
            table = Table(name, MetaData(), *(Column(column[1]) for column in columns))
            statement = select(table).order_by(*table.c)
            digest, count = hashlib.sha256(), 0
            for row in connection.execute(str(statement.compile(dialect=sqlite_dialect()))):
                digest.update(json.dumps(row, separators=(",", ":"), default=str).encode())
                digest.update(b"\n")
                count += 1
            tables[name] = {"count": count, "sha256": digest.hexdigest()}
        audits = connection.execute("SELECT COUNT(*) FROM read_access_audits").fetchone()[0]
        subject_counts = dict(
            connection.execute(
                "SELECT subject, COUNT(*) FROM read_access_audits GROUP BY subject"
            ).fetchall()
        )
    return {
        "business_tables": tables,
        "read_audit_count": audits,
        "audit_by_subject": subject_counts,
    }
