"""Real SQLite/API review ledger; browser observations here are explicit fixtures."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from scripts.document_review_evidence import document_numeric_review_evidence
from sqlalchemy import delete, select

from windops_backend.models import CommandReceipt, KnowledgeDocument, KnowledgePassage


@pytest_asyncio.fixture
async def reviewed_source(app, client):
    text = "+123.45 kN"
    identity = str(uuid4())
    uri = f"minio://windops-knowledge-documents/documents/{identity}/source.pdf"
    content = (Path(__file__).parent / "fixtures/document/scanned-zh.pdf").read_bytes()
    source_sha = app.state.artifact_verifier.register_object(uri, content, "application/pdf")
    passage_id = str(uuid4())
    async with app.state.session_factory() as session:
        session.add(
            KnowledgeDocument(
                id=identity,
                title="Explicit SQLite evidence fixture",
                document_type="manual",
                body=text,
                citation_uri=uri,
                artifact_uri=uri,
                artifact_sha256=source_sha,
                content_type="application/pdf",
                content_size_bytes=len(content),
                document_version="fixture-v1",
                ingestion_status="indexed",
                vectorized=False,
            )
        )
        await session.flush()
        session.add(
            KnowledgePassage(
                id=passage_id,
                document_id=identity,
                ordinal=0,
                document_version="fixture-v1",
                source_sha256=source_sha,
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                parser_version="explicit-fixture.v1",
                page_number=2,
                native_locator={"source_verified": True},
                char_start=0,
                char_end=len(text),
                text=text,
            )
        )
        await session.commit()
    endpoint = f"/api/v1/knowledge/passages/{passage_id}/numeric-review"
    response = await client.get(endpoint)
    assert response.status_code == 200, response.text
    binding = response.json()["source_binding"]
    proposal_key = str(uuid4())
    proposal = {
        **binding,
        "expected_revision": 0,
        "kind": "correct_values",
        "reason": "Synthetic wrong proposal must be rejected",
        "corrections": [
            {
                "start": 0,
                "end": 7,
                "original_value": "+123.45",
                "corrected_value": "+123.40",
                "original_unit": "kN",
                "corrected_unit": "kN",
            }
        ],
    }

    async def post(path, body, subject, key=None):
        result = await client.post(
            path,
            json=body,
            headers={
                "X-WindOps-Test-Principal": subject,
                "Idempotency-Key": key or str(uuid4()),
            },
        )
        assert result.status_code == 200, result.text
        return result

    await post(endpoint, proposal, "business-manager", proposal_key)
    await post(
        endpoint + "/decision",
        {
            **binding,
            "expected_revision": 1,
            "action": "reject",
            "reason": "Original PDF is +123.45",
        },
        "business-reviewer",
    )
    await post(
        endpoint,
        {
            **binding,
            "expected_revision": 2,
            "kind": "confirm_original",
            "corrections": [],
            "reason": "Confirm original synthetic PDF value",
        },
        "business-manager",
    )
    response = await post(
        endpoint + "/decision",
        {
            **binding,
            "expected_revision": 3,
            "action": "approve",
            "reason": "Independent fixture PDF inspection",
        },
        "business-reviewer",
    )
    result = response.json()
    replay = await post(endpoint, proposal, "business-manager", proposal_key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    browser = {
        "numeric_review": {
            "passage_id": passage_id,
            "review_id": result["current"]["review_id"],
            "source_binding": binding,
            "history_hashes": [item["snapshot_sha256"] for item in result["history"]],
            "rejected_incorrect_correction": True,
            "original_text_unchanged": True,
            "pdf_rendered": True,
            "self_review_rejected": True,
            "replay_preserved_history": True,
            "stale_revision_rejected": True,
            "restricted_read_rejected": True,
            "reloaded_persisted_state": True,
        }
    }
    return browser, passage_id


async def test_database_evidence_rehashes_real_review_history_and_receipts(app, reviewed_source):
    browser, passage_id = reviewed_source
    async with app.state.session_factory() as session:
        passage = await session.get(KnowledgePassage, passage_id)
        result = await document_numeric_review_evidence(session, browser, [passage])
    assert (
        result["revision"]
        == result["history_events"]
        == result["command_receipts"]
        == result["domain_events"]
        == 4
    )
    assert result["snapshots_rehashed_from_database"] is True


@pytest.mark.parametrize(
    "field", ["pdf_rendered", "restricted_read_rejected", "replay_preserved_history"]
)
async def test_acceptance_never_promotes_missing_browser_observation(app, reviewed_source, field):
    browser, passage_id = reviewed_source
    browser["numeric_review"][field] = 1  # bool-only: truthy is not evidence.
    async with app.state.session_factory() as session:
        passage = await session.get(KnowledgePassage, passage_id)
        with pytest.raises(RuntimeError, match=field):
            await document_numeric_review_evidence(session, browser, [passage])


async def test_acceptance_rejects_changed_browser_history_hash(app, reviewed_source):
    browser, passage_id = reviewed_source
    browser["numeric_review"]["history_hashes"][0] = "0" * 64
    async with app.state.session_factory() as session:
        passage = await session.get(KnowledgePassage, passage_id)
        with pytest.raises(RuntimeError, match="browser history"):
            await document_numeric_review_evidence(session, browser, [passage])


async def test_acceptance_rejects_missing_durable_command_receipt(app, reviewed_source):
    browser, passage_id = reviewed_source
    async with app.state.session_factory() as session:
        receipt_id = await session.scalar(
            select(CommandReceipt.id).where(CommandReceipt.target == passage_id).limit(1)
        )
        await session.execute(delete(CommandReceipt).where(CommandReceipt.id == receipt_id))
        await session.commit()
        passage = await session.get(KnowledgePassage, passage_id)
        with pytest.raises(RuntimeError, match="receipt count"):
            await document_numeric_review_evidence(session, browser, [passage])
