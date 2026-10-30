"""Bounded document HTTP state machine. Terminal API claims need separate SQL evidence.

Only registered loopback API and MinIO endpoints are used. The report contains no
paths, credentials, URLs, original contents or response bodies. Upload byte counters
mean bytes supplied to the transport, not an assertion about remote durable storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from windops_backend.operations.load_runner import (
    PrivateUser,
    _loopback_origin,
    _quiet_transport,
    _settle,
    _strict_json,
)

MIB = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_RUN = re.compile(r"[a-f0-9]{32}\Z")
StageName = Literal["source", "presign", "upload", "ingest", "poll"]
STAGES: tuple[StageName, ...] = ("source", "presign", "upload", "ingest", "poll")
StageOutcome = Literal["not_run", "success", "failure", "timeout", "cancelled", "unknown"]
DocumentOutcome = Literal["not_run", "indexed", "failed", "cancelled", "submission_unknown"]


def new_document_id(run_id: str) -> str:
    if not _RUN.fullmatch(run_id):
        raise ValueError("invalid document run identity")
    return f"load-{run_id[:12]}-{uuid4().hex[:16]}"


@dataclass(frozen=True)
class DocumentInput:
    document_id: str
    user_id: str
    path: Path = field(repr=False)
    artifact_sha256: str
    size_bytes: int
    tenant_id: str = field(repr=False)
    wind_farm_id: str = field(repr=False)
    turbine_id: str = field(repr=False)
    parser: Literal["native", "docling"] = "docling"
    document_version: str = "1"
    data_scope: Literal["knowledge"] = "knowledge"

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.document_id, 64),
            (self.user_id, 64),
            (self.tenant_id, 64),
            (self.wind_farm_id, 36),
            (self.turbine_id, 32),
            (self.document_version, 32),
        ):
            if not isinstance(value, str) or len(value) > maximum or not _ID.fullmatch(value):
                raise ValueError("invalid document identity")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("an absolute workspace PDF path is required")
        if not _SHA.fullmatch(self.artifact_sha256):
            raise ValueError("invalid PDF SHA256")
        if type(self.size_bytes) is not int or not 0 < self.size_bytes <= 50 * MIB:
            raise ValueError("invalid PDF byte size")
        if self.parser not in {"native", "docling"} or self.data_scope != "knowledge":
            raise ValueError("unsupported document parser or scope")


class DocumentBatchLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    concurrency: int = Field(default=2, ge=1, le=8)
    maximum_documents: int = Field(default=32, ge=1, le=32)
    maximum_http_requests: int = Field(default=2000, ge=1, le=20_000)
    maximum_total_seconds: float = Field(default=900.0, gt=0, le=900)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=30)
    poll_interval_seconds: float = Field(default=1.0, ge=0.01, le=30)
    maximum_polls_per_document: int = Field(default=120, ge=1, le=1000)
    maximum_document_bytes: int = Field(default=50 * MIB, ge=1, le=50 * MIB)
    maximum_total_upload_bytes: int = Field(default=100 * MIB, ge=1, le=1600 * MIB)
    maximum_response_bytes: int = Field(default=MIB, ge=1, le=24 * MIB)
    maximum_total_response_bytes: int = Field(default=64 * MIB, ge=1, le=256 * MIB)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.request_timeout_seconds > self.maximum_total_seconds:
            raise ValueError("request timeout exceeds batch deadline")
        return self


@dataclass
class DocumentStageSample:
    stage: StageName
    outcome: StageOutcome = "not_run"
    started_monotonic: float | None = None
    finished_monotonic: float | None = None
    error_code: str | None = None


@dataclass
class DocumentHttpSample:
    sequence: int
    document_id: str
    user_id: str
    stage: StageName
    outcome: StageOutcome = "not_run"
    started_monotonic: float | None = None
    finished_monotonic: float | None = None
    http_status: int | None = None
    request_bytes: int | None = None
    response_bytes: int | None = None
    error_code: str | None = None


@dataclass
class DocumentResult:
    document_id: str
    user_id: str
    parser: str
    artifact_sha256: str
    size_bytes: int
    stages: tuple[DocumentStageSample, ...]
    outcome: DocumentOutcome = "not_run"
    accepted: bool | None = False
    ingestion_status: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class DocumentBatchResult:
    run_id: str
    documents: tuple[DocumentResult, ...]
    http_samples: tuple[DocumentHttpSample, ...]
    elapsed_seconds: float
    completed: bool
    cleanup_passed: bool
    stop_reason: str | None
    total_upload_bytes: int
    total_response_bytes: int
    max_observed_inflight: int


class _Failure(Exception):
    def __init__(self, code: str, outcome: StageOutcome = "failure") -> None:
        self.code, self.outcome = code, outcome
        super().__init__(code)


def _workspace_file(path: Path, root: Path) -> None:
    if not root.is_absolute() or not path.is_relative_to(root):
        raise _Failure("source_outside_workspace")
    for candidate in (root, *path.relative_to(root).parents):
        resolved = candidate if candidate.is_absolute() else root / candidate
        if resolved.is_symlink() or resolved.is_junction():
            raise _Failure("source_link_forbidden")
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise _Failure("source_invalid")
    if not path.resolve().is_relative_to(root.resolve()):
        raise _Failure("source_outside_workspace")


def _verify_source(item: DocumentInput, root: Path) -> None:
    _workspace_file(item.path, root)
    digest, count = hashlib.sha256(), 0
    with item.path.open("rb") as stream:
        first = stream.read(5)
        if first != b"%PDF-":
            raise _Failure("source_not_pdf")
        digest.update(first)
        count += len(first)
        while block := stream.read(64 * 1024):
            count += len(block)
            if count > item.size_bytes:
                raise _Failure("source_size_mismatch")
            digest.update(block)
    if count != item.size_bytes:
        raise _Failure("source_size_mismatch")
    if digest.hexdigest() != item.artifact_sha256:
        raise _Failure("source_sha_mismatch")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise _Failure("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _Failure("invalid_timestamp") from None
    if result.utcoffset() is None:
        raise _Failure("invalid_timestamp")
    return result.astimezone(UTC)


def _grant(value: object, item: DocumentInput, origin: str, bucket: str) -> tuple[str, str]:
    try:
        if not isinstance(value, dict) or value.get("document_id") != item.document_id:
            raise ValueError
        upload, artifact = value.get("upload_url"), value.get("artifact_uri")
        if not isinstance(upload, str) or not isinstance(artifact, str) or len(upload) > 8192:
            raise ValueError
        url, uri = urlsplit(upload), urlsplit(artifact)
        base = urlsplit(origin)
        path = f"/documents/{item.document_id}/"
        if (
            (url.scheme, url.netloc) != (base.scheme, base.netloc)
            or url.username is not None
            or url.password is not None
            or url.fragment
            or "%" in url.path
            or "\\" in upload
            or any(char.isspace() for char in upload)
            or uri.scheme != "minio"
            or uri.netloc != bucket
            or uri.query
            or uri.fragment
            or not uri.path.startswith(path)
            or not re.fullmatch(r"[a-f0-9-]{36}-source\.pdf", uri.path[len(path) :])
            or url.path != f"/{bucket}{uri.path}"
            or value.get("required_headers") != {"content-type": "application/pdf"}
        ):
            raise ValueError
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        required = {
            "X-Amz-Algorithm",
            "X-Amz-Credential",
            "X-Amz-Date",
            "X-Amz-Expires",
            "X-Amz-SignedHeaders",
            "X-Amz-Signature",
        }
        if set(query) != required or any(len(values) != 1 for values in query.values()):
            raise ValueError
        if (
            query["X-Amz-Algorithm"] != ["AWS4-HMAC-SHA256"]
            or query["X-Amz-SignedHeaders"] != ["host"]
            or _SHA.fullmatch(query["X-Amz-Signature"][0]) is None
        ):
            raise ValueError
        signed = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        lifetime = int(query["X-Amz-Expires"][0])
        credential = query["X-Amz-Credential"][0].split("/")
        if (
            not 1 <= lifetime <= 900
            or len(credential) != 5
            or not credential[0]
            or credential[1] != signed.strftime("%Y%m%d")
            or not credential[2]
            or credential[3:] != ["s3", "aws4_request"]
        ):
            raise ValueError
        expires, now = _timestamp(value.get("expires_at")), datetime.now(UTC)
        if (
            not now < expires <= now + timedelta(seconds=905)
            or signed > now + timedelta(seconds=5)
            or abs((expires - signed - timedelta(seconds=lifetime)).total_seconds()) > 5
        ):
            raise ValueError
        return upload, artifact
    except (ValueError, KeyError, TypeError, _Failure):
        raise _Failure("invalid_upload_grant") from None


def _document_state(value: object, item: DocumentInput, artifact: str) -> str:
    expected = {
        "document_id": item.document_id,
        "tenant_id": item.tenant_id,
        "wind_farm_id": item.wind_farm_id,
        "turbine_id": item.turbine_id,
        "data_scope": item.data_scope,
        "document_version": item.document_version,
        "artifact_uri": artifact,
        "artifact_sha256": item.artifact_sha256,
        "content_type": "application/pdf",
        "content_size_bytes": item.size_bytes,
    }
    if not isinstance(value, dict) or any(
        value.get(key) != expected_value for key, expected_value in expected.items()
    ):
        raise _Failure("server_identity_mismatch")
    if (
        type(value.get("content_size_bytes")) is not int
        or type(value.get("vectorized")) is not bool
    ):
        raise _Failure("server_identity_mismatch")
    state = value.get("ingestion_status")
    if not isinstance(state, str) or state not in {
        "pending_parse",
        "parsing",
        "pending",
        "indexed",
        "parse_failed",
        "failed",
    }:
        raise _Failure("invalid_document_state")
    parse = value.get("parse_state")
    if item.parser == "docling":
        if not isinstance(parse, dict) or not isinstance(parse.get("identity"), dict):
            raise _Failure("server_parser_mismatch")
        identity = parse["identity"]
        if (
            identity.get("method") != "docling_pdf_v1"
            or identity.get("source_sha256") != item.artifact_sha256
            or identity.get("document_version") != item.document_version
        ):
            raise _Failure("server_parser_mismatch")
    elif parse is not None:
        raise _Failure("server_parser_mismatch")
    if state == "indexed":
        _timestamp(value.get("indexed_at"))
        if (
            value.get("vectorized") is not True
            or not isinstance(value.get("embedding_provider"), str)
            or not value["embedding_provider"]
            or not isinstance(value.get("embedding_model"), str)
            or not value["embedding_model"]
            or (
                item.parser == "docling"
                and (not isinstance(parse, dict) or parse.get("status") != "parsed")
            )
        ):
            raise _Failure("invalid_indexed_state")
    elif value["vectorized"] is not False:
        raise _Failure("invalid_document_state")
    return str(state)


class _Batch:
    def __init__(
        self,
        inputs: tuple[DocumentInput, ...],
        users: tuple[PrivateUser, ...],
        limits: DocumentBatchLimits,
        root: Path,
        api: str,
        minio: str,
        bucket: str,
        verify: ssl.SSLContext | bool,
    ) -> None:
        self.inputs, self.users, self.limits, self.root = inputs, users, limits, root
        self.api, self.minio, self.bucket, self.verify = api, minio, bucket, verify
        self.started = time.perf_counter()
        self.stop_reason: str | None = None
        self.documents = tuple(
            DocumentResult(
                item.document_id,
                item.user_id,
                item.parser,
                item.artifact_sha256,
                item.size_bytes,
                tuple(DocumentStageSample(stage) for stage in STAGES),
            )
            for item in inputs
        )
        self.http: list[DocumentHttpSample] = []
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.object_client: httpx.AsyncClient | None = None
        self.artifacts: dict[str, str] = {}
        self.tasks: set[asyncio.Task[None] | asyncio.Task[bool]] = set()
        self.upload_bytes = self.response_bytes = self.reserved_response = self.inflight = (
            self.max_inflight
        ) = 0

    def now(self) -> float:
        return time.perf_counter() - self.started

    def check(self) -> None:
        if self.now() >= self.limits.maximum_total_seconds and self.stop_reason is None:
            self.stop_reason = "batch_deadline_exceeded"
        if self.stop_reason:
            raise _Failure(self.stop_reason, "cancelled")

    async def stream_pdf(
        self, item: DocumentInput, sample: DocumentHttpSample
    ) -> AsyncIterator[bytes]:
        _workspace_file(item.path, self.root)
        digest, size = hashlib.sha256(), 0
        with item.path.open("rb") as stream:
            while block := await asyncio.to_thread(stream.read, 64 * 1024):
                self.check()
                size += len(block)
                if size > item.size_bytes:
                    raise _Failure("source_size_mismatch")
                digest.update(block)
                sample.request_bytes = size
                self.upload_bytes += len(block)
                yield block
        if size != item.size_bytes or digest.hexdigest() != item.artifact_sha256:
            raise _Failure("source_changed_during_upload")

    async def request(
        self,
        item: DocumentInput,
        stage: StageName,
        method: str,
        target: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[object, DocumentHttpSample]:
        self.check()
        if len(self.http) >= self.limits.maximum_http_requests:
            raise _Failure("http_request_budget_exhausted")
        cap = min(
            self.limits.maximum_response_bytes,
            self.limits.maximum_total_response_bytes - self.response_bytes - self.reserved_response,
        )
        if cap <= 0:
            raise _Failure("response_byte_budget_exhausted")
        self.reserved_response += cap
        sample = DocumentHttpSample(len(self.http), item.document_id, item.user_id, stage)
        self.http.append(sample)
        body = (
            b""
            if payload is None
            else json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        try:
            if len(body) > 16 * 1024:
                raise _Failure("request_body_limit")
            if stage == "upload":
                client = self.object_client
                headers = {
                    "content-type": "application/pdf",
                    "content-length": str(item.size_bytes),
                }
                content: bytes | AsyncIterator[bytes] = self.stream_pdf(item, sample)
            else:
                user = next(user for user in self.users if user.alias == item.user_id)
                try:
                    headers = dict(user.request_headers(method, target, body))
                except Exception:
                    raise _Failure("request_identity_failed") from None
                if stage in {"presign", "ingest"}:
                    headers["Idempotency-Key"] = f"{item.document_id}-{stage}-{uuid4().hex[:12]}"
                if body:
                    headers["Content-Type"] = "application/json"
                client, content = self.clients[item.user_id], body
            if client is None:
                raise _Failure("client_setup_failed")
            sample.started_monotonic = self.now()
            sample.request_bytes = 0 if stage == "upload" else len(body)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            async with asyncio.timeout(self.limits.request_timeout_seconds):
                async with client.stream(
                    method, target, headers=headers, content=content
                ) as response:
                    sample.http_status, sample.response_bytes = response.status_code, 0
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > cap:
                        raise _Failure("response_too_large")
                    raw = bytearray()
                    # One reserved bounded chunk also handles a missing Content-Length.
                    # At that limit an unframed response is rejected conservatively,
                    # without reading an extra probe byte outside the shared budget.
                    async for block in response.aiter_raw(chunk_size=cap):
                        sample.response_bytes += len(block)
                        self.response_bytes += len(block)
                        if sample.response_bytes > cap or (
                            sample.response_bytes == cap and not length.isdigit()
                        ):
                            raise _Failure("response_too_large")
                        raw.extend(block)
                    if (stage == "ingest" and response.status_code != 202) or (
                        stage != "ingest" and not 200 <= response.status_code < 300
                    ):
                        raise _Failure("http_status")
                    if (
                        stage == "ingest"
                        and response.headers.get("idempotency-replayed") != "false"
                    ):
                        raise _Failure("document_not_fresh")
                    if stage == "upload":
                        value: object = None
                    else:
                        try:
                            value = _strict_json(bytes(raw))
                        except (ValueError, RecursionError):
                            raise _Failure("json_invalid") from None
            sample.outcome = "success"
            return value, sample
        except (TimeoutError, httpx.TimeoutException):
            sample.outcome, sample.error_code = "timeout", "request_timeout"
            raise _Failure("request_timeout", "timeout") from None
        except asyncio.CancelledError:
            sample.outcome, sample.error_code = "cancelled", self.stop_reason or "request_cancelled"
            raise
        except _Failure as failure:
            sample.outcome, sample.error_code = failure.outcome, failure.code
            raise
        except Exception:
            sample.outcome, sample.error_code = "failure", "transport_error"
            raise _Failure("transport_error") from None
        finally:
            self.reserved_response -= cap
            if sample.started_monotonic is not None:
                sample.finished_monotonic = self.now()
                self.inflight -= 1

    async def phase(
        self, index: int, name: StageName, operation: Callable[[], Awaitable[None]]
    ) -> bool:
        result = self.documents[index]
        stage = next(stage for stage in result.stages if stage.stage == name)
        try:
            self.check()
            stage.started_monotonic = self.now()
            await operation()
            stage.outcome = "success"
            return True
        except asyncio.CancelledError:
            stage.outcome, stage.error_code = "cancelled", self.stop_reason or "request_cancelled"
        except _Failure as failure:
            stage.outcome, stage.error_code = failure.outcome, failure.code
        except Exception:
            stage.outcome, stage.error_code = "failure", "stage_failed"
        finally:
            if stage.started_monotonic is not None:
                stage.finished_monotonic = self.now()
            else:
                stage.outcome = "not_run"
        result.error_code = stage.error_code
        result.outcome = "cancelled" if stage.outcome == "cancelled" else "failed"
        if name == "ingest":
            attempts = [
                sample
                for sample in self.http
                if sample.document_id == result.document_id and sample.stage == "ingest"
            ]
            if attempts and attempts[-1].started_monotonic is not None:
                sample = attempts[-1]
                if (
                    sample.http_status is None
                    or sample.http_status >= 500
                    or 200 <= sample.http_status < 400
                ):
                    result.accepted = None
                    result.outcome, stage.outcome = "submission_unknown", "unknown"
        return False

    async def submit(self, index: int) -> None:
        item = self.inputs[index]
        upload: str = ""
        artifact: str = ""

        async def presign() -> None:
            nonlocal upload, artifact
            value, sample = await self.request(
                item,
                "presign",
                "POST",
                "/api/v1/knowledge/documents/uploads/presign",
                {
                    "document_id": item.document_id,
                    "file_name": "source.pdf",
                    "content_type": "application/pdf",
                    "artifact_sha256": item.artifact_sha256,
                },
            )
            try:
                upload, artifact = _grant(value, item, self.minio, self.bucket)
            except _Failure as failure:
                sample.outcome, sample.error_code = "failure", failure.code
                raise

        async def put() -> None:
            await self.request(item, "upload", "PUT", upload)

        async def ingest() -> None:
            value, sample = await self.request(
                item,
                "ingest",
                "POST",
                "/api/v1/knowledge/documents",
                {
                    "document_id": item.document_id,
                    "title": "Performance fixture PDF",
                    "document_type": "performance_fixture",
                    "document_version": item.document_version,
                    "artifact_uri": artifact,
                    "artifact_sha256": item.artifact_sha256,
                    "content_type": "application/pdf",
                    "parser": item.parser,
                    "tenant_id": item.tenant_id,
                    "wind_farm_id": item.wind_farm_id,
                    "turbine_id": item.turbine_id,
                    "data_scope": item.data_scope,
                },
            )
            try:
                state = _document_state(value, item, artifact)
                if not isinstance(value, dict) or value.get("replayed") is not False:
                    raise _Failure("document_not_fresh")
            except _Failure as failure:
                sample.outcome, sample.error_code = "failure", failure.code
                raise
            result = self.documents[index]
            result.accepted, result.ingestion_status = True, state
            self.artifacts[item.document_id] = artifact

        steps: tuple[tuple[StageName, Callable[[], Awaitable[None]]], ...] = (
            ("presign", presign),
            ("upload", put),
            ("ingest", ingest),
        )
        for name, operation in steps:
            if not await self.phase(index, name, operation):
                return

    async def poll(self, index: int) -> None:
        item, result = self.inputs[index], self.documents[index]
        if result.accepted is not True:
            return

        async def observe() -> None:
            for poll in range(self.limits.maximum_polls_per_document):
                if poll:
                    await asyncio.sleep(self.limits.poll_interval_seconds)
                value, sample = await self.request(
                    item, "poll", "GET", f"/api/v1/knowledge/documents/{item.document_id}"
                )
                try:
                    state = _document_state(value, item, self.artifacts[item.document_id])
                except _Failure as failure:
                    sample.outcome, sample.error_code = "failure", failure.code
                    raise
                result.ingestion_status = state
                if state == "indexed":
                    result.outcome = "indexed"
                    return
                if state in {"parse_failed", "failed"}:
                    raise _Failure(
                        "document_parse_failed"
                        if state == "parse_failed"
                        else "document_index_failed"
                    )
            raise _Failure("poll_budget_exhausted")

        await self.phase(index, "poll", observe)

    async def workers(self, operation: Callable[[int], Awaitable[None]]) -> None:
        cursor = 0

        async def worker() -> None:
            nonlocal cursor
            while cursor < len(self.inputs) and self.stop_reason is None:
                index, cursor = cursor, cursor + 1
                await operation(index)

        tasks = [
            asyncio.create_task(worker())
            for _ in range(min(self.limits.concurrency, len(self.inputs)))
        ]
        self.tasks.update(tasks)
        await asyncio.gather(*tasks)

    async def execute(self, after_submissions: Callable[[], Awaitable[None]] | None) -> None:
        for index, item in enumerate(self.inputs):

            async def check_source(item: DocumentInput = item) -> None:
                await asyncio.to_thread(_verify_source, item, self.root)

            if not await self.phase(index, "source", check_source):
                if self.stop_reason is None:
                    self.stop_reason = "source_preflight_failed"
                return
        for user in self.users:
            self.clients[user.alias] = httpx.AsyncClient(
                base_url=self.api,
                verify=self.verify,
                trust_env=False,
                follow_redirects=False,
                timeout=self.limits.request_timeout_seconds,
                headers={"Accept-Encoding": "identity"},
                limits=httpx.Limits(
                    max_connections=self.limits.concurrency,
                    max_keepalive_connections=self.limits.concurrency,
                ),
            )
        self.object_client = httpx.AsyncClient(
            verify=self.verify,
            trust_env=False,
            follow_redirects=False,
            timeout=self.limits.request_timeout_seconds,
            headers={"Accept-Encoding": "identity"},
            limits=httpx.Limits(max_connections=self.limits.concurrency),
        )
        await self.workers(self.submit)
        if after_submissions is not None and self.stop_reason is None:
            try:
                await after_submissions()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stop_reason = "after_submissions_failed"
                return
        await self.workers(self.poll)


async def run_document_batch(
    *,
    run_id: str,
    api_origin: str,
    minio_origin: str,
    minio_bucket: str,
    users: Sequence[PrivateUser],
    documents: Sequence[DocumentInput],
    limits: DocumentBatchLimits,
    workspace_root: Path,
    verify: ssl.SSLContext | bool = True,
    cancel_event: asyncio.Event | None = None,
    after_submissions: Callable[[], Awaitable[None]] | None = None,
) -> DocumentBatchResult:
    """Submit each document once, then poll; 202 alone never yields indexed success.

    after_submissions is a trusted owned-worker control hook, under the same batch
    deadline. It may restart an offline worker; no user-supplied code is loaded.
    """
    api, minio = _loopback_origin(api_origin), _loopback_origin(minio_origin)
    if not _RUN.fullmatch(run_id) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", minio_bucket):
        raise ValueError("invalid owned batch identity")
    if verify is not True and (
        not isinstance(verify, ssl.SSLContext)
        or verify.verify_mode != ssl.CERT_REQUIRED
        or not verify.check_hostname
    ):
        raise ValueError("TLS verification must remain enabled")
    if len(users) != 4 or len({user.alias for user in users}) != 4:
        raise ValueError("four distinct authorized user aliases are required")
    if not 1 <= len(documents) <= limits.maximum_documents or len(
        {item.document_id for item in documents}
    ) != len(documents):
        raise ValueError("invalid bounded document plan")
    for item in documents:
        if not re.fullmatch(
            f"load-{run_id[:12]}-[a-f0-9]{{16}}", item.document_id
        ) or item.user_id not in {user.alias for user in users}:
            raise ValueError("document identity is not owned by this run")
        if item.size_bytes > limits.maximum_document_bytes:
            raise ValueError("document exceeds its byte budget")
    if sum(item.size_bytes for item in documents) > limits.maximum_total_upload_bytes:
        raise ValueError("document plan exceeds the upload budget")
    batch = _Batch(
        tuple(documents), tuple(users), limits, workspace_root, api, minio, minio_bucket, verify
    )
    cleanup_passed = False
    with _quiet_transport() as unsettled:
        execution: asyncio.Task[None] | None = None
        cancellation: asyncio.Task[bool] | None = None
        try:
            if cancel_event is not None and cancel_event.is_set():
                batch.stop_reason = "request_cancelled"
            else:
                execution = asyncio.create_task(batch.execute(after_submissions))
                batch.tasks.add(execution)
                if cancel_event is not None:
                    cancellation = asyncio.create_task(cancel_event.wait())
                    batch.tasks.add(cancellation)
                watching = {execution} if cancellation is None else {execution, cancellation}
                done, _ = await asyncio.wait(
                    watching,
                    timeout=limits.maximum_total_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution in done:
                    execution.result()
                    batch.check()
                else:
                    batch.stop_reason = (
                        "request_cancelled" if cancellation in done else "batch_deadline_exceeded"
                    )
        except asyncio.CancelledError:
            batch.stop_reason = "request_cancelled"
        except _Failure as failure:
            batch.stop_reason = failure.code
        except Exception:
            batch.stop_reason = "batch_setup_failed"
        finally:
            elapsed = batch.now()
            deadline = time.perf_counter() + 1.0

            async def cleanup() -> bool:
                settled = await _settle(
                    list(batch.tasks), deadline=deadline - 0.5, cancel=True, unsettled=unsettled
                )
                clients = [
                    *batch.clients.values(),
                    *([batch.object_client] if batch.object_client else []),
                ]
                closed = await _settle(
                    [asyncio.create_task(client.aclose()) for client in clients],
                    deadline=deadline,
                    cancel=False,
                    unsettled=unsettled,
                )
                return settled and closed

            finishing = asyncio.create_task(cleanup())
            while not finishing.done():
                try:
                    await asyncio.shield(finishing)
                except asyncio.CancelledError:
                    batch.stop_reason = "request_cancelled"
            try:
                cleanup_passed = finishing.result()
            except Exception:
                cleanup_passed = False
    if not cleanup_passed and batch.stop_reason is None:
        batch.stop_reason = "client_cleanup_failed"
    for result in batch.documents:
        if result.outcome == "not_run":
            result.error_code = batch.stop_reason or "not_started"
            if result.accepted:
                result.outcome = (
                    "cancelled" if batch.stop_reason == "request_cancelled" else "failed"
                )
        for stage in result.stages:
            if stage.outcome == "not_run":
                stage.error_code = result.error_code or batch.stop_reason or "not_started"
    return DocumentBatchResult(
        run_id,
        batch.documents,
        tuple(batch.http),
        elapsed,
        all(result.outcome == "indexed" for result in batch.documents)
        and cleanup_passed
        and batch.stop_reason is None,
        cleanup_passed,
        batch.stop_reason,
        batch.upload_bytes,
        batch.response_bytes,
        batch.max_inflight,
    )
