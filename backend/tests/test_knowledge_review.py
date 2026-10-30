"""Actual SQLite/API review behavior over synthetic PDF source bytes, not OCR accuracy."""

from __future__ import annotations

import asyncio
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from test_knowledge_passages import ingest
from windops_backend.access_control import attach_access_policy
from windops_backend.document_parse_result_identity import document_parse_result_hash
from windops_backend.errors import InvalidTransitionError
from windops_backend.knowledge_graph.domain import GraphAccessPolicy
from windops_backend.models import (
    CommandReceipt,
    KnowledgeDocument,
    KnowledgeNumericReview,
    KnowledgeNumericReviewEvent,
    KnowledgePassage,
)
from windops_backend.read_audit import ReadAuditEnqueueError
from windops_backend.schema_knowledge_review import NumericCorrection
from windops_backend.services import knowledge_review
from windops_backend.services.knowledge_review import apply_corrections

MANAGER = {
    "X-WindOps-Test-Principal": "numeric-author",
    "X-WindOps-Test-Role": "operations_manager",
    "X-WindOps-Test-Tenant-Ids": "tenant-east-china",
    "X-WindOps-Test-Data-Scopes": "knowledge",
}
REVIEWER = {
    **MANAGER,
    "X-WindOps-Test-Principal": "numeric-reviewer",
    "X-WindOps-Test-Role": "maintenance_reviewer",
}


def source_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    page = writer.add_blank_page(width=300, height=400)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (Force +123.45 kN; offset -0.25 mm.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


async def prepared(app, client):
    document = await ingest(
        app, client, source_pdf(), "application/pdf", tenant_id="tenant-east-china"
    )
    passages = (
        await client.get(f"/api/v1/knowledge/documents/{document['document_id']}/passages")
    ).json()["passages"]
    passage = passages[0]
    url = f"/api/v1/knowledge/passages/{passage['passage_id']}/numeric-review"
    response = await client.get(url, headers=MANAGER)
    assert response.status_code == 200, response.text
    detail = response.json()
    value_start = passage["text"].index("+123.45")
    unit_start = passage["text"].index("kN")
    payload = {
        **detail["source_binding"],
        "expected_revision": 0,
        "kind": "correct_values",
        "corrections": [
            {
                "start": value_start,
                "end": value_start + 7,
                "original_value": "+123.45",
                "corrected_value": "+123.40",
                "original_unit": "kN",
                "corrected_unit": "kN",
                "unit_start": unit_start,
                "unit_end": unit_start + 2,
            }
        ],
        "reason": "Synthetic comparison with the same original PDF.",
    }
    return document, passage, url, detail, payload


async def command(client, url, payload, *, headers=MANAGER, key=None):
    return await client.post(
        url, json=payload, headers={**headers, "Idempotency-Key": key or str(uuid4())}
    )


def decision(payload, revision, action="approve"):
    return {
        key: value
        for key, value in payload.items()
        if key.startswith("expected_") and key != "expected_revision"
    } | {
        "expected_revision": revision,
        "action": action,
        "reason": "Independent original PDF verification.",
    }


async def test_source_review_history_replay_and_originals_remain_immutable(app, client):
    document, passage, url, empty, payload = await prepared(app, client)
    assert empty["current"] is None and empty["history"] == []
    assert empty["permissions"] == {
        "can_propose": True,
        "can_approve": False,
        "can_reject": False,
        "can_withdraw": False,
    }
    async with app.state.session_factory() as session:
        doc = await session.get(KnowledgeDocument, document["document_id"])
        row = await session.get(KnowledgePassage, passage["passage_id"])
        before = (
            doc.body,
            doc.artifact_sha256,
            doc.metadata_,
            row.text,
            row.text_sha256,
            row.native_locator,
            row.parser_version,
            row.embedding,
        )
    key = str(uuid4())
    proposed = await command(client, url, payload, key=key)
    assert proposed.status_code == 200, proposed.text
    first = proposed.json()
    assert first["current"]["revision"] == 1
    assert first["current"]["corrected_text"].startswith("Force +123.40 kN")
    assert first["current"]["status"] == "pending"
    assert not first["updates_retrieval_text"] and not first["authorizes_work"]
    same_author = await command(client, url + "/decision", decision(payload, 1))
    assert same_author.status_code == 422
    approved = await command(client, url + "/decision", decision(payload, 1), headers=REVIEWER)
    assert approved.status_code == 200, approved.text
    assert approved.json()["current"]["status"] == "approved"
    assert approved.json()["current"]["revision"] == 2
    assert approved.json()["current"]["reviewed_by"] == "numeric-reviewer"
    replay = await command(client, url, payload, key=key)
    assert replay.status_code == 200 and replay.json() == first
    assert replay.headers["Idempotency-Replayed"] == "true"
    stale = await command(client, url, payload)
    assert stale.status_code == 409
    changed_key_payload = {**payload, "reason": "A different explanation."}
    assert (await command(client, url, changed_key_payload, key=key)).status_code == 409
    current = (await client.get(url + "?limit=1", headers=MANAGER)).json()
    assert current["current"]["revision"] == 2
    assert current["next_cursor"] == 2
    assert [row["revision"] for row in current["history"]] == [2]
    older = (await client.get(url + "?cursor=2&limit=1", headers=MANAGER)).json()
    assert [row["revision"] for row in older["history"]] == [1]
    assert older["next_cursor"] is None
    for event in current["history"] + older["history"]:
        assert event["snapshot_sha256"] == document_parse_result_hash(event["snapshot"])
    async with app.state.session_factory() as session:
        doc = await session.get(KnowledgeDocument, document["document_id"])
        row = await session.get(KnowledgePassage, passage["passage_id"])
        assert before == (
            doc.body,
            doc.artifact_sha256,
            doc.metadata_,
            row.text,
            row.text_sha256,
            row.native_locator,
            row.parser_version,
            row.embedding,
        )
        assert (
            await session.scalar(select(func.count()).select_from(KnowledgeNumericReviewEvent)) == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.command_type.like("knowledge.numeric-review.%"))
            )
            == 2
        )


async def test_revision_resubmission_rejection_withdrawal_and_read_permissions(app, client):
    _, _, url, _, payload = await prepared(app, client)
    await command(client, url, payload)
    allowed = (await client.get(url, headers=REVIEWER)).json()["permissions"]
    assert allowed == {
        "can_propose": True,
        "can_approve": True,
        "can_reject": True,
        "can_withdraw": False,
    }
    rejected = await command(
        client, url + "/decision", decision(payload, 1, "reject"), headers=REVIEWER
    )
    assert rejected.status_code == 200 and rejected.json()["current"]["status"] == "rejected"
    assert (
        await command(client, url + "/decision", decision(payload, 2), headers=REVIEWER)
    ).status_code == 422
    resubmitted = await command(
        client,
        url,
        {**payload, "expected_revision": 2, "kind": "confirm_original", "corrections": []},
    )
    assert resubmitted.status_code == 200
    assert resubmitted.json()["current"]["revision"] == 3
    assert resubmitted.json()["current"]["reviewed_by"] is None
    withdrawn = await command(client, url + "/decision", decision(payload, 3, "withdraw"))
    assert withdrawn.status_code == 200 and withdrawn.json()["current"]["status"] == "withdrawn"
    assert [event["action"] for event in withdrawn.json()["history"]] == [
        "withdraw",
        "propose",
        "reject",
        "propose",
    ]


@pytest.mark.parametrize(
    "field",
    [
        "expected_document_version",
        "expected_source_sha256",
        "expected_text_sha256",
        "expected_parser_version",
        "expected_locator_sha256",
    ],
)
async def test_wrong_binding_is_rejected_even_for_receipt_replay(app, client, field):
    _, _, url, _, payload = await prepared(app, client)
    key = str(uuid4())
    assert (await command(client, url, payload, key=key)).status_code == 200
    bad = {**payload, field: "0" * 64 if "sha256" in field else "different"}
    assert (await command(client, url, bad, key=key)).status_code == 409
    async with app.state.session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(KnowledgeNumericReviewEvent)) == 1
        )


async def test_current_scope_is_rechecked_before_receipt_and_private_object_io(
    app, client, monkeypatch
):
    document, passage, url, _, payload = await prepared(app, client)
    key = str(uuid4())
    assert (await command(client, url, payload, key=key)).status_code == 200

    async def forbidden_read(*args, **kwargs):
        pytest.fail("unauthorized request reached object storage")

    monkeypatch.setattr(app.state.artifact_verifier, "read_verified_object", forbidden_read)
    revoked = {**MANAGER, "X-WindOps-Test-Tenant-Ids": "another-tenant"}
    for method, endpoint, body in (
        ("GET", url, None),
        ("POST", url, payload),
        ("POST", url + "/decision", decision(payload, 1)),
    ):
        response = await client.request(
            method, endpoint, json=body, headers={**revoked, "Idempotency-Key": key}
        )
        assert response.status_code == 404
    wrong_domain = {**MANAGER, "X-WindOps-Test-Data-Scopes": "structural"}
    assert (await command(client, url, payload, headers=wrong_domain, key=key)).status_code == 403
    for roles in ("scada_ingestor", "agent_worker"):
        assert (
            await command(client, url, payload, headers={**MANAGER, "X-WindOps-Test-Role": roles})
        ).status_code == 403
    async with app.state.session_factory() as session:
        receipt = await session.scalar(
            select(CommandReceipt).where(CommandReceipt.idempotency_key == key)
        )
        assert receipt.replay_count == 0
        attach_access_policy(
            session,
            GraphAccessPolicy.from_values(tenant_ids=["another-tenant"], data_scopes=["knowledge"]),
        )
        assert list(await session.scalars(select(KnowledgeNumericReview))) == []
        assert list(await session.scalars(select(KnowledgeNumericReviewEvent))) == []


async def test_object_drift_rejects_approval_without_partial_state_receipt_or_audit(app, client):
    document, _, url, _, payload = await prepared(app, client)
    await command(client, url, payload)
    app.state.artifact_verifier.register_object(
        document["artifact_uri"], b"changed", "application/pdf"
    )
    denied = await command(client, url + "/decision", decision(payload, 1), headers=REVIEWER)
    assert denied.status_code == 422
    current = (await client.get(url, headers=REVIEWER)).json()
    assert current["current"]["status"] == "pending" and current["current"]["revision"] == 1
    async with app.state.session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(KnowledgeNumericReviewEvent)) == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.command_type == "knowledge.numeric-review.decide.v1")
            )
            == 0
        )


async def test_event_failure_rolls_back_head_and_command_receipt(app, client, monkeypatch):
    _, _, url, _, payload = await prepared(app, client)

    def fail_event(*args, **kwargs):
        raise RuntimeError("injected event persistence failure")

    monkeypatch.setattr(knowledge_review, "append_domain_event", fail_event)
    with pytest.raises(RuntimeError, match="event persistence"):
        await command(client, url, payload)
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeNumericReview)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(KnowledgeNumericReviewEvent)) == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.command_type.like("knowledge.numeric-review.%"))
            )
            == 0
        )


async def test_read_audit_admission_fails_closed(app, client):
    _, _, url, _, _ = await prepared(app, client)

    class FailedSink:
        async def record(self, _event):
            raise ReadAuditEnqueueError("READ_AUDIT_BACKPRESSURE", "synthetic backpressure")

    app.state.read_audit_sink = FailedSink()
    assert (await client.get(url, headers=MANAGER)).status_code == 503


async def test_actual_sqlite_two_sessions_conflict_and_history_cannot_be_updated_or_deleted(
    app, client
):
    _, _, url, _, payload = await prepared(app, client)
    results = await asyncio.gather(command(client, url, payload), command(client, url, payload))
    assert sorted(row.status_code for row in results) == [200, 409]
    for statement in (
        update(KnowledgeNumericReviewEvent).values(reason="tampered"),
        delete(KnowledgeNumericReviewEvent),
    ):
        async with app.state.session_factory() as session:
            with pytest.raises(IntegrityError, match="immutable"):
                await session.execute(statement)
            await session.rollback()
    async with app.state.session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(KnowledgeNumericReviewEvent)) == 1
        )


def test_codepoint_offsets_preserve_sign_precision_unicode_and_explicit_units():
    text = "中文😀 +123.45 kN"
    item = NumericCorrection(
        start=4,
        end=11,
        original_value="+123.45",
        corrected_value="-123.400",
        original_unit="kN",
        corrected_unit="N",
        unit_start=12,
        unit_end=14,
    )
    assert apply_corrections(text, [item]) == "中文😀 -123.400 N"
    external_unit = NumericCorrection(
        start=0,
        end=6,
        original_value="I23.45",
        corrected_value="123.45",
        original_unit="kN",
        corrected_unit="kN",
    )
    assert apply_corrections("I23.45", [external_unit]) == "123.45"
    assert apply_corrections("I23.45.", [external_unit]) == "123.45."
    with pytest.raises(InvalidTransitionError):
        apply_corrections(text, [item, item])


@pytest.mark.parametrize(
    "changes",
    [
        {"corrected_value": 123.4},
        {"corrected_value": "NaN"},
        {"original_value": "replace all text"},
        {"unit_start": 8},
        {"corrected_unit": "N"},
        {"start": True},
        {"end": 0},
        {"corrected_value": "123.45"},
        {"original_unit": " "},
    ],
)
def test_numeric_contract_rejects_non_numeric_ambiguous_or_noop_inputs(changes):
    raw = {
        "start": 0,
        "end": 6,
        "original_value": "123.45",
        "corrected_value": "123.40",
        "original_unit": "kN",
        "corrected_unit": "kN",
        **changes,
    }
    with pytest.raises(ValidationError):
        NumericCorrection.model_validate(raw)


async def test_unverified_or_non_pdf_sources_and_invalid_spans_are_rejected(app, client):
    doc = await ingest(app, client, b"force 123.45 kN", tenant_id="tenant-east-china")
    piece = (await client.get(f"/api/v1/knowledge/documents/{doc['document_id']}/passages")).json()[
        "passages"
    ][0]
    assert (
        await client.get(
            f"/api/v1/knowledge/passages/{piece['passage_id']}/numeric-review", headers=MANAGER
        )
    ).status_code == 422
    _, _, url, _, payload = await prepared(app, client)
    bad = {**payload, "corrections": [{**payload["corrections"][0], "start": 0}]}
    assert (await command(client, url, bad)).status_code == 422


async def test_same_key_concurrency_replays_once_and_decision_cas_has_one_winner(app, client):
    _, _, url, _, payload = await prepared(app, client)
    key = str(uuid4())
    first = await asyncio.gather(
        command(client, url, payload, key=key),
        command(client, url, payload, key=key),
    )
    assert [row.status_code for row in first] == [200, 200]
    assert first[0].json() == first[1].json()
    assert sorted(row.headers["Idempotency-Replayed"] for row in first) == ["false", "true"]
    second_reviewer = {**REVIEWER, "X-WindOps-Test-Principal": "another-reviewer"}
    decisions = await asyncio.gather(
        command(client, url + "/decision", decision(payload, 1), headers=REVIEWER),
        command(client, url + "/decision", decision(payload, 1, "reject"), headers=second_reviewer),
    )
    assert sorted(row.status_code for row in decisions) == [200, 409]
    detail = (await client.get(url, headers=MANAGER)).json()
    assert detail["current"]["revision"] == 2
    assert len(detail["history"]) == 2


@pytest.mark.parametrize("drift", ["parser", "locator", "text", "version"])
async def test_actual_persisted_source_drift_blocks_successful_receipt_replay(app, client, drift):
    document, passage, url, _, payload = await prepared(app, client)
    key = str(uuid4())
    assert (await command(client, url, payload, key=key)).status_code == 200
    async with app.state.session_factory() as session, session.begin():
        piece = await session.get(KnowledgePassage, passage["passage_id"])
        if drift == "parser":
            piece.parser_version = "changed-parser"
        elif drift == "locator":
            piece.native_locator = {**piece.native_locator, "page_number": 1}
        elif drift == "text":
            piece.text += "tampered"
        else:
            document_row = await session.get(KnowledgeDocument, document["document_id"])
            document_row.document_version = "changed-version"
    assert (await command(client, url, payload, key=key)).status_code == 409
    async with app.state.session_factory() as session:
        receipt = await session.scalar(
            select(CommandReceipt).where(CommandReceipt.idempotency_key == key)
        )
        assert receipt.replay_count == 0


async def test_document_entity_grant_and_anonymous_review_access(app, client):
    document, _, url, _, payload = await prepared(app, client)
    scoped = {
        "X-WindOps-Test-Principal": "entity-reader",
        "X-WindOps-Test-Role": "maintenance_reviewer",
        "X-WindOps-Test-Entity-Ids": document["document_id"].lower(),
        "X-WindOps-Test-Data-Scopes": "knowledge_documents",
    }
    assert (await client.get(url, headers=scoped)).status_code == 200
    assert (await command(client, url, payload, headers=scoped)).status_code == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        assert (await anonymous.get(url)).status_code == 401
    bad = {**payload, "corrections": payload["corrections"] * 2}
    assert (await command(client, url, bad)).status_code == 422
