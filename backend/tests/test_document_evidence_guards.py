"""Real SQLite failures must still be rejected when evidence tooling runs with -O."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from windops_backend.models import KnowledgeDocument


@pytest.mark.parametrize(
    "violation", ["indexed_document", "failed_document_status", "failed_document_empty"]
)
async def test_optimized_evidence_process_rejects_invalid_persisted_state(app, tmp_path, violation):
    document = KnowledgeDocument(
        id="guard-valid",
        title="unit persisted document",
        document_type="manual",
        body="unit",
        citation_uri="unit://valid",
        ingestion_status="indexed",
        vectorized=True,
    )
    failed = KnowledgeDocument(
        id="guard-failed",
        title="unit failed document",
        document_type="manual",
        body="",
        citation_uri="unit://failed",
        ingestion_status="parse_failed",
        vectorized=False,
    )
    if violation == "indexed_document":
        document.ingestion_status = "pending"
    elif violation == "failed_document_status":
        failed.vectorized = True
    else:
        failed.body = "must not be published"
    async with app.state.session_factory() as session:
        session.add_all([document, failed])
        await session.commit()
    (tmp_path / "document-browser.json").write_text(
        json.dumps(
            {
                "document_id": document.id,
                "failed_document_id": failed.id,
            }
        )
    )
    script = """
import asyncio, sys
from pathlib import Path
from scripts.document_e2e_evidence import document_database_evidence
try:
    asyncio.run(document_database_evidence({'database_url': sys.argv[1]}, Path(sys.argv[2])))
except RuntimeError as exc:
    if str(exc) != 'document evidence verification failed: ' + sys.argv[3]:
        raise
    print('rejected invalid persisted state with optimization enabled')
else:
    raise RuntimeError('invalid persisted state was accepted')
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-O",
            "-c",
            script,
            app.state.settings.database_url,
            str(tmp_path),
            violation,
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "rejected invalid persisted state" in result.stdout
    assert not (tmp_path / "document-parsed-result.json").exists()
