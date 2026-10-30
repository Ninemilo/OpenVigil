"""Persisted topology count failures survive independent Python optimization."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from windops_backend.models import TowerComponent


@pytest.mark.parametrize("optimization", [0, 1, 2])
async def test_invalid_entity_counts_are_rejected_by_independent_evidence_process(
    app: FastAPI, tmp_path: Path, optimization: int
) -> None:
    async with app.state.session_factory() as session:
        component_count = await session.scalar(select(func.count()).select_from(TowerComponent))
    # The real test database has no acceptance topology; the first gate requires two components.
    assert component_count == 0
    for name in ("structural-browser.json", "scope-fixture.json"):
        (tmp_path / name).write_text(json.dumps({}), encoding="utf-8")
    script = """
import asyncio, sys
from pathlib import Path
from scripts.structural_scope_evidence import scope_database_evidence
if sys.flags.optimize != int(sys.argv[3]):
    raise RuntimeError('optimization mode was not applied')
try:
    config = {'environment': 'test', 'database_url': sys.argv[1]}
    asyncio.run(scope_database_evidence(config, Path(sys.argv[2])))
except RuntimeError as exc:
    if str(exc) != 'structural scope evidence verification failed: entity_counts':
        raise
    print('rejected invalid persisted topology before downstream evidence')
else:
    raise RuntimeError('invalid persisted topology was accepted')
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            *(["-" + "O" * optimization] if optimization else []),
            "-c",
            script,
            app.state.settings.database_url,
            str(tmp_path),
            str(optimization),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "rejected invalid persisted topology before downstream evidence" in result.stdout
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TowerComponent)) == 0
