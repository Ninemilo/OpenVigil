"""Real loopback HTTP fixtures; synthetic PDF/JSON do not verify OCR or MinIO itself."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import pytest

from windops_backend.operations.load_documents import (
    DocumentBatchLimits,
    DocumentBatchResult,
    DocumentInput,
    new_document_id,
    run_document_batch,
)
from windops_backend.operations.load_runner import PrivateUser

PDF = b"%PDF-1.4\ntransport-only-private-original\n%%EOF\n"
SECRET = "transport-fixture-private-secret"
RUN = "1a" * 16
BUCKET = "owned-load-fixture"
PREFIX = "/api/v1/knowledge/documents"


def signed(method: str, target: str, body: bytes) -> dict[str, str]:
    digest = hashlib.sha256(method.encode() + target.encode() + body).hexdigest()
    return {"Authorization": f"Bearer {SECRET}", "X-Fixture-Binding": digest}


def users() -> tuple[PrivateUser, ...]:
    return tuple(PrivateUser(f"u{i}", {"X-Fixture-Alias": f"u{i}"}, signed) for i in range(4))


def documents(root: Path, count: int = 1) -> tuple[DocumentInput, ...]:
    path = root / "private-source.pdf"
    path.write_bytes(PDF)
    return tuple(
        DocumentInput(
            document_id=new_document_id(RUN),
            user_id=f"u{i % 4}",
            path=path,
            artifact_sha256=hashlib.sha256(PDF).hexdigest(),
            size_bytes=len(PDF),
            tenant_id="private-tenant",
            wind_farm_id=f"private-farm-{i % 2}",
            turbine_id=f"private-turbine-{i % 2}",
        )
        for i in range(count)
    )


@dataclass
class Request:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass
class State:
    api: str = ""
    minio: str = ""
    requests: list[tuple[str, Request]] = field(default_factory=list)
    sources: dict[str, DocumentInput] = field(default_factory=dict)
    grants: dict[str, str] = field(default_factory=dict)
    uploaded: dict[str, bytes] = field(default_factory=dict)
    submissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    held: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    mode: str = "normal"
    grant_fault: str = ""
    ready: bool = True
    active: int = 0
    maximum_active: int = 0

    def response(self, document_id: str, *, indexed: bool) -> dict[str, Any]:
        item = self.sources[document_id]
        return {
            "document_id": item.document_id,
            "tenant_id": item.tenant_id,
            "wind_farm_id": item.wind_farm_id,
            "turbine_id": item.turbine_id,
            "data_scope": "knowledge",
            "document_version": "1",
            "artifact_uri": self.grants[document_id],
            "artifact_sha256": item.artifact_sha256,
            "content_type": "application/pdf",
            "content_size_bytes": item.size_bytes,
            "vectorized": indexed,
            "ingestion_status": "indexed" if indexed else "pending_parse",
            "embedding_provider": "synthetic-fixture" if indexed else None,
            "embedding_model": "transport-only" if indexed else None,
            "indexed_at": datetime.now(UTC).isoformat() if indexed else None,
            "parse_state": {
                "identity": {
                    "method": "docling_pdf_v1",
                    "source_sha256": item.artifact_sha256,
                    "document_version": "1",
                },
                "status": "parsed" if indexed else "pending",
            }
            if item.parser == "docling"
            else None,
            "replayed": False,
        }

    def grant(self, document_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).replace(microsecond=0)
        path = f"/documents/{document_id}/{uuid4()}-source.pdf"
        self.grants[document_id] = f"minio://{BUCKET}{path}"
        query = urlencode(
            {
                "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                "X-Amz-Credential": f"{SECRET}/{now:%Y%m%d}/us-east-1/s3/aws4_request",
                "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"),
                "X-Amz-Expires": "900",
                "X-Amz-SignedHeaders": "host",
                "X-Amz-Signature": "a" * 64,
            }
        )
        grant: dict[str, Any] = {
            "document_id": document_id,
            "artifact_uri": self.grants[document_id],
            "upload_url": f"{self.minio}/{BUCKET}{path}?{query}",
            "expires_at": (now + timedelta(seconds=900)).isoformat(),
            "required_headers": {"content-type": "application/pdf"},
        }
        if self.grant_fault == "origin":
            grant["upload_url"] = grant["upload_url"].replace(self.minio, self.api)
        elif self.grant_fault == "bucket":
            grant["upload_url"] = grant["upload_url"].replace(BUCKET, "foreign-bucket")
        elif self.grant_fault == "document":
            grant["upload_url"] = grant["upload_url"].replace(document_id, "foreign-document")
        elif self.grant_fault == "path":
            grant["upload_url"] = grant["upload_url"].replace("source.pdf", "%2e%2e.pdf")
        elif self.grant_fault == "headers":
            grant["required_headers"]["Authorization"] = SECRET
        elif self.grant_fault == "expired":
            grant["expires_at"] = (now - timedelta(seconds=1)).isoformat()
        elif self.grant_fault == "signature":
            grant["upload_url"] += "&X-Amz-Signature=" + "b" * 64
        elif self.grant_fault == "lifetime":
            grant["upload_url"] = grant["upload_url"].replace("Expires=900", "Expires=901")
        elif self.grant_fault == "identity":
            grant["document_id"] = "foreign-document"
        return grant


Reply = tuple[int, object, dict[str, str]]


@asynccontextmanager
async def fixture() -> AsyncIterator[State]:
    state = State()
    handlers: set[asyncio.Task[None]] = set()

    async def dispatch(kind: str, request: Request) -> Reply:
        state.requests.append((kind, request))
        if kind == "object":
            assert request.method == "PUT"
            assert "authorization" not in request.headers
            assert "x-fixture-binding" not in request.headers
            assert "x-fixture-alias" not in request.headers
            document_id = urlsplit(request.target).path.split("/")[3]
            state.uploaded[document_id] = request.body
            return 200, b"", {}
        expected = signed(request.method, request.target, request.body)
        assert request.headers["authorization"] == expected["Authorization"]
        assert request.headers["x-fixture-binding"] == expected["X-Fixture-Binding"]
        assert request.headers["x-fixture-alias"] in {f"u{i}" for i in range(4)}
        if request.method == "POST":
            assert request.headers["idempotency-key"]
            data = json.loads(request.body)
            document_id = data["document_id"]
            if request.target.endswith("/presign"):
                if state.mode == "bad_presign_json":
                    return 200, b'{"duplicate":1,"duplicate":2}', {}
                if state.mode == "http_error":
                    return 503, {"private": SECRET}, {}
                if state.mode == "large_response":
                    return 200, b"x" * 100_000, {}
                if state.mode == "streamed_response":
                    return 200, b"x" * 100_000, {"Transfer-Encoding": "chunked"}
                if state.mode == "slow_presign":
                    state.held.set()
                    await state.release.wait()
                await asyncio.sleep(0.015)
                if state.mode == "change_file":
                    state.sources[document_id].path.write_bytes(PDF.replace(b"private", b"changed"))
                return 200, state.grant(document_id), {}
            assert request.target == PREFIX
            assert (
                hashlib.sha256(state.uploaded[document_id]).hexdigest() == data["artifact_sha256"]
            )
            state.submissions[document_id] = data
            if state.mode == "hold_ingest":
                state.held.set()
                await state.release.wait()
            if state.mode == "bad_ingest_json":
                return 202, b"private malformed body", {"Idempotency-Replayed": "false"}
            value = state.response(document_id, indexed=False)
            if state.mode == "wrong_ingest_identity":
                value["artifact_sha256"] = "0" * 64
            if state.mode == "replayed":
                value["replayed"] = True
            status = 200 if state.mode == "wrong_ingest_status" else 202
            return status, value, {"Idempotency-Replayed": "false"}
        document_id = request.target.rsplit("/", 1)[1]
        state.polls[document_id] = state.polls.get(document_id, 0) + 1
        assert state.ready, "poll before after_submissions hook"
        if state.mode == "hold_poll":
            state.held.set()
            await state.release.wait()
        indexed = state.mode != "pending" and state.polls[document_id] >= 2
        value = state.response(document_id, indexed=indexed)
        if state.mode == "wrong_poll_identity":
            value["turbine_id"] = "foreign-turbine"
        elif state.mode in {"parse_failed", "failed"}:
            value["ingestion_status"] = state.mode
        elif state.mode == "invalid_indexed":
            value["ingestion_status"] = "indexed"
            value["indexed_at"] = datetime.now(UTC).isoformat()
        return 200, value, {}

    async def serve(kind: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        counted = False
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            lines = header.split("\r\n")
            method, target, _ = lines[0].split(" ")
            # Keep original header values: JWT/base64 and SHA bindings are case-sensitive.
            headers = {
                line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                for line in lines[1:]
                if line
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            state.active += 1
            counted = True
            state.maximum_active = max(state.maximum_active, state.active)
            status, value, extra = await dispatch(kind, Request(method, target, headers, body))
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            response = (
                f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n"
            )
            if extra.get("Transfer-Encoding") == "chunked":
                response = f"HTTP/1.1 {status} Fixture\r\nConnection: close\r\n"
                raw = f"{len(raw):x}\r\n".encode() + raw + b"\r\n0\r\n\r\n"
            response += "".join(f"{name}: {value}\r\n" for name, value in extra.items())
            writer.write(response.encode() + b"\r\n" + raw)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass  # The client intentionally aborts fault cases; no test assertion is suppressed.
        except Exception as exc:
            state.errors.append(type(exc).__name__)
        finally:
            if counted:
                state.active -= 1
            writer.close()
            handlers.discard(task)

    api = await asyncio.start_server(lambda r, w: serve("api", r, w), "127.0.0.1", 0)
    storage = await asyncio.start_server(lambda r, w: serve("object", r, w), "127.0.0.1", 0)
    state.api = f"http://127.0.0.1:{api.sockets[0].getsockname()[1]}"
    state.minio = f"http://127.0.0.1:{storage.sockets[0].getsockname()[1]}"
    try:
        yield state
    finally:
        api.close()
        storage.close()
        state.release.set()
        pending = list(handlers)
        for handler in pending:
            handler.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await api.wait_closed()
        await storage.wait_closed()
        assert state.errors == []


async def run(
    state: State,
    root: Path,
    items: tuple[DocumentInput, ...],
    *,
    cancel: asyncio.Event | None = None,
    hook: Callable[[], Awaitable[None]] | None = None,
    principals: tuple[PrivateUser, ...] | None = None,
    **overrides: Any,
) -> DocumentBatchResult:
    state.sources = {item.document_id: item for item in items}
    options: dict[str, Any] = {
        "maximum_total_seconds": 8.0,
        "request_timeout_seconds": 1.0,
        "poll_interval_seconds": 0.01,
        "maximum_polls_per_document": 3,
    }
    options.update(overrides)
    return await run_document_batch(
        run_id=RUN,
        api_origin=state.api,
        minio_origin=state.minio,
        minio_bucket=BUCKET,
        users=principals or users(),
        documents=items,
        limits=DocumentBatchLimits(**options),
        workspace_root=root,
        cancel_event=cancel,
        after_submissions=hook,
    )


async def test_real_http_upload_signing_scope_hook_and_concurrency(tmp_path: Path) -> None:
    items = documents(tmp_path, 8)
    async with fixture() as state:
        state.ready = False

        async def restart() -> None:
            assert len(state.submissions) == 8
            assert not state.polls
            state.ready = True

        result = await run(state, tmp_path, items, hook=restart, concurrency=3)
    assert result.completed and result.cleanup_passed
    assert all(doc.outcome == "indexed" and doc.accepted is True for doc in result.documents)
    assert all(stage.outcome == "success" for doc in result.documents for stage in doc.stages)
    assert len(result.http_samples) == 40
    assert 2 <= result.max_observed_inflight <= 3
    assert 2 <= state.maximum_active <= 3
    assert result.total_upload_bytes == 8 * len(PDF)
    assert all(value == PDF for value in state.uploaded.values())
    assert {
        request.headers["x-fixture-alias"] for kind, request in state.requests if kind == "api"
    } == {f"u{i}" for i in range(4)}
    for sample in result.http_samples:
        assert sample.started_monotonic is not None and sample.finished_monotonic is not None
        assert 0 <= sample.started_monotonic <= sample.finished_monotonic <= result.elapsed_seconds


@pytest.mark.parametrize(
    "fault",
    [
        "origin",
        "bucket",
        "document",
        "path",
        "headers",
        "expired",
        "signature",
        "lifetime",
        "identity",
    ],
)
async def test_untrusted_grant_never_reaches_put(tmp_path: Path, fault: str) -> None:
    async with fixture() as state:
        state.grant_fault = fault
        result = await run(state, tmp_path, documents(tmp_path))
    assert not result.completed and result.cleanup_passed
    assert len(state.requests) == 1
    assert result.documents[0].error_code == "invalid_upload_grant"
    assert result.documents[0].stages[2].outcome == "not_run"


@pytest.mark.parametrize("fault", ["sha", "size", "type", "outside"])
async def test_source_preflight_rejects_before_any_network(tmp_path: Path, fault: str) -> None:
    item = documents(tmp_path)[0]
    root = tmp_path
    if fault == "sha":
        item = replace(item, artifact_sha256="0" * 64)
    elif fault == "size":
        item = replace(item, size_bytes=len(PDF) - 1)
    elif fault == "type":
        item.path.write_bytes(PDF.replace(b"%PDF-", b"not-p"))
    else:
        root = tmp_path / "owned-subfolder"
        root.mkdir()
    async with fixture() as state:
        result = await run(state, root, (item,))
    assert not state.requests and not result.http_samples
    assert result.stop_reason == "source_preflight_failed"


async def test_changed_bytes_during_upload_are_never_submitted(tmp_path: Path) -> None:
    async with fixture() as state:
        state.mode = "change_file"
        result = await run(state, tmp_path, documents(tmp_path))
    assert not state.submissions
    assert result.documents[0].error_code == "source_changed_during_upload"
    assert not result.completed


@pytest.mark.parametrize(
    "mode,code",
    [
        ("bad_presign_json", "json_invalid"),
        ("http_error", "http_status"),
        ("large_response", "response_too_large"),
    ],
)
async def test_presign_http_json_and_response_bounds(tmp_path: Path, mode: str, code: str) -> None:
    async with fixture() as state:
        state.mode = mode
        result = await run(state, tmp_path, documents(tmp_path), maximum_response_bytes=4096)
    assert result.documents[0].error_code == code
    assert len(state.requests) == 1
    assert result.total_response_bytes <= 4096


@pytest.mark.parametrize(
    "mode,code",
    [
        ("bad_ingest_json", "json_invalid"),
        ("wrong_ingest_identity", "server_identity_mismatch"),
        ("wrong_ingest_status", "http_status"),
        ("replayed", "document_not_fresh"),
    ],
)
async def test_unverifiable_accepted_response_is_unknown_without_retry(
    tmp_path: Path, mode: str, code: str
) -> None:
    async with fixture() as state:
        state.mode = mode
        result = await run(state, tmp_path, documents(tmp_path))
    assert len(state.submissions) == 1 and not state.polls
    doc = result.documents[0]
    assert doc.outcome == "submission_unknown" and doc.accepted is None
    assert doc.error_code == code and doc.stages[3].outcome == "unknown"
    assert len(result.http_samples) == 3


async def test_timeout_after_server_received_submission_is_unknown(tmp_path: Path) -> None:
    async with fixture() as state:
        state.mode = "hold_ingest"
        result = await run(state, tmp_path, documents(tmp_path), request_timeout_seconds=0.15)
    assert len(state.submissions) == 1 and not state.polls
    assert result.documents[0].outcome == "submission_unknown"
    assert result.http_samples[-1].outcome == "timeout"
    assert result.cleanup_passed and result.elapsed_seconds < 2


@pytest.mark.parametrize("mode", ["hold_ingest", "hold_poll"])
async def test_cancellation_retains_every_document_and_stage(tmp_path: Path, mode: str) -> None:
    cancel = asyncio.Event()
    async with fixture() as state:
        state.mode = mode
        task = asyncio.create_task(
            run(state, tmp_path, documents(tmp_path, 4), cancel=cancel, concurrency=1)
        )
        await asyncio.wait_for(state.held.wait(), timeout=4)
        cancel.set()
        result = await asyncio.wait_for(task, timeout=3)
    assert result.stop_reason == "request_cancelled" and result.cleanup_passed
    assert len(result.documents) == 4 and all(len(doc.stages) == 5 for doc in result.documents)
    if mode == "hold_ingest":
        assert result.documents[0].outcome == "submission_unknown"
        assert all(doc.outcome == "not_run" for doc in result.documents[1:])
    else:
        assert all(doc.accepted is True for doc in result.documents)
        assert all(doc.outcome == "cancelled" for doc in result.documents)


async def test_external_task_cancellation_returns_ledger(tmp_path: Path) -> None:
    async with fixture() as state:
        state.mode = "hold_ingest"
        task = asyncio.create_task(run(state, tmp_path, documents(tmp_path)))
        await asyncio.wait_for(state.held.wait(), timeout=4)
        task.cancel()
        result = await asyncio.wait_for(task, timeout=3)
    assert result.stop_reason == "request_cancelled"
    assert result.documents[0].outcome == "submission_unknown"
    assert result.cleanup_passed


@pytest.mark.parametrize(
    "mode,code",
    [
        ("pending", "poll_budget_exhausted"),
        ("wrong_poll_identity", "server_identity_mismatch"),
        ("parse_failed", "document_parse_failed"),
        ("failed", "document_index_failed"),
        ("invalid_indexed", "invalid_indexed_state"),
    ],
)
async def test_202_and_terminal_failures_are_not_indexed_success(
    tmp_path: Path, mode: str, code: str
) -> None:
    async with fixture() as state:
        state.mode = mode
        result = await run(state, tmp_path, documents(tmp_path))
    doc = result.documents[0]
    assert doc.accepted is True and doc.outcome == "failed"
    assert doc.error_code == code and not result.completed
    assert len(result.http_samples) <= 6


async def test_global_deadline_covers_worker_restart_hook(tmp_path: Path) -> None:
    async with fixture() as state:

        async def restart() -> None:
            await state.release.wait()

        started = time.perf_counter()
        result = await run(
            state,
            tmp_path,
            documents(tmp_path),
            hook=restart,
            maximum_total_seconds=0.5,
            request_timeout_seconds=0.2,
        )
        elapsed = time.perf_counter() - started
    assert result.stop_reason == "batch_deadline_exceeded" and elapsed < 2
    assert result.documents[0].accepted is True and not state.polls
    assert result.documents[0].stages[-1].outcome == "not_run"


async def test_callback_errors_are_safe_and_keep_accepted_work(tmp_path: Path) -> None:
    async def broken() -> None:
        raise RuntimeError(SECRET)

    async with fixture() as state:
        result = await run(state, tmp_path, documents(tmp_path), hook=broken)
    assert result.stop_reason == "after_submissions_failed"
    assert result.documents[0].accepted is True and not state.polls
    assert SECRET not in repr(result)


async def test_http_count_budget_stops_before_next_request(tmp_path: Path) -> None:
    async with fixture() as state:
        result = await run(state, tmp_path, documents(tmp_path), maximum_http_requests=3)
    assert len(state.requests) == len(result.http_samples) == 3
    assert not state.polls and result.documents[0].accepted is True
    assert result.documents[0].error_code == "http_request_budget_exhausted"


async def test_no_sensitive_transport_logs_or_report_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with fixture() as state:
        result = await run(state, tmp_path, documents(tmp_path))
    assert result.completed
    public = json.dumps(asdict(result)) + repr(result) + caplog.text
    for private in (
        SECRET,
        "private-source.pdf",
        "private-tenant",
        "private-farm",
        "private-original",
        "X-Amz-",
        "artifact_uri",
        "127.0.0.1:",
    ):
        assert private not in public


async def test_authentication_callback_failure_never_sends_request(tmp_path: Path) -> None:
    def broken(*_: object) -> dict[str, str]:
        raise RuntimeError(SECRET)

    principals = tuple(PrivateUser(f"u{i}", {}, broken) for i in range(4))
    async with fixture() as state:
        result = await run(state, tmp_path, documents(tmp_path), principals=principals)
    assert not state.requests
    assert result.documents[0].error_code == "request_identity_failed"


@pytest.mark.parametrize("fault", ["duplicate", "foreign_id", "upload_bytes", "document_bytes"])
async def test_invalid_plan_rejected_before_http(tmp_path: Path, fault: str) -> None:
    items = documents(tmp_path)
    options: dict[str, Any] = {}
    if fault == "duplicate":
        items = (items[0], items[0])
    elif fault == "foreign_id":
        items = (replace(items[0], document_id="foreign-document"),)
    elif fault == "upload_bytes":
        options["maximum_total_upload_bytes"] = 1
    else:
        options["maximum_document_bytes"] = 1
    async with fixture() as state:
        with pytest.raises(ValueError):
            await run(state, tmp_path, items, **options)
        assert not state.requests


def test_document_ids_are_fresh_per_invocation() -> None:
    assert new_document_id(RUN) != new_document_id(RUN)
    with pytest.raises(ValueError):
        new_document_id("invalid")


async def test_chunked_response_stays_within_shared_bytes_without_extra_probe(
    tmp_path: Path,
) -> None:
    async with fixture() as state:
        state.mode = "streamed_response"
        result = await run(
            state,
            tmp_path,
            documents(tmp_path, 4),
            concurrency=4,
            maximum_response_bytes=1024,
            maximum_total_response_bytes=2048,
        )
    assert not result.completed and result.cleanup_passed
    assert result.total_response_bytes == 2048
    assert all((sample.response_bytes or 0) <= 1024 for sample in result.http_samples)
    assert all(
        doc.error_code in {"response_too_large", "response_byte_budget_exhausted"}
        for doc in result.documents
    )


async def test_cancelled_before_start_retains_entire_not_run_plan(tmp_path: Path) -> None:
    cancel = asyncio.Event()
    cancel.set()
    async with fixture() as state:
        result = await run(state, tmp_path, documents(tmp_path, 4), cancel=cancel)
    assert not state.requests and not result.http_samples
    assert result.cleanup_passed and result.stop_reason == "request_cancelled"
    assert all(doc.outcome == "not_run" and doc.accepted is False for doc in result.documents)
    assert all(
        stage.outcome == "not_run" and stage.started_monotonic is None
        for doc in result.documents
        for stage in doc.stages
    )


async def test_native_parser_can_complete_without_fabricated_parse_state(tmp_path: Path) -> None:
    item = replace(documents(tmp_path)[0], parser="native")
    async with fixture() as state:
        result = await run(state, tmp_path, (item,))
    assert result.completed and result.documents[0].parser == "native"
