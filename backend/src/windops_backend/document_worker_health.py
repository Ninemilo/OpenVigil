"""Bounded, expiring OCR worker evidence without importing OCR or its broker."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from windops_backend.config import Settings
from windops_backend.document_parse_identity import DOCUMENT_PARSER_VERSIONS, parser_code_identity
from windops_backend.document_parse_process import PACKAGE_PATH, _bounded_read

HEARTBEAT_TTL_SECONDS = 45
HEARTBEAT_INTERVAL_SECONDS = 10
PREFLIGHT_INTERVAL_SECONDS = 300
PREFLIGHT_RETRY_SECONDS = 60
PREFLIGHT_TIMEOUT_SECONDS = 90
PREFLIGHT_MAX_AGE_SECONDS = 360
MAX_WORKERS = 32
MAX_RECORD_BYTES = 16 * 1024
REDIS_TIMEOUT_SECONDS = 2
REGISTRY_KEY = "windops:{document-worker-health}:registry:v1"
WORKER_KEY_PREFIX = "windops:{document-worker-health}:worker:"
CONTRACT = "openvigil.document-worker-health.v1"
logger = logging.getLogger(__name__)

# Redis owns the clock and lease. Both scripts have fixed record/iteration bounds.
PUBLISH_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local record = cjson.decode(ARGV[2])
record.observed_epoch = now
redis.call('SET', KEYS[2], cjson.encode(record), 'EX', ARGV[3])
redis.call('ZADD', KEYS[1], now, ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[3]),
                          'LIMIT', 0, 64)
if #expired > 0 then redis.call('ZREM', KEYS[1], unpack(expired)) end
return 1
"""
READ_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local ids = redis.call('ZREVRANGEBYSCORE', KEYS[1], '+inf', now - tonumber(ARGV[1]),
                       'LIMIT', 0, tonumber(ARGV[2]) + 1)
if #ids > tonumber(ARGV[2]) then return {tostring(now), 'limit'} end
local result = {tostring(now), 'ok'}
for _, id in ipairs(ids) do
  if #id ~= 32 or string.find(id, '[^0-9a-f]') then
    return {tostring(now), 'invalid'}
  end
  local key = ARGV[3] .. id
  local size = redis.call('STRLEN', key)
  if size > tonumber(ARGV[4]) then return {tostring(now), 'invalid'} end
  local ttl = redis.call('PTTL', key)
  if size > 0 and ttl > 0 then
    table.insert(result, redis.call('GET', key))
    table.insert(result, tostring(ttl))
  end
end
return result
"""

PREFLIGHT_BOOTSTRAP = """
import contextlib, importlib.util, json, pathlib, sys
package_path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('windops_backend', package_path / '__init__.py',
    submodule_search_locations=[str(package_path)])
if spec is None or spec.loader is None: raise RuntimeError('package unavailable')
package = importlib.util.module_from_spec(spec)
sys.modules['windops_backend'] = package
spec.loader.exec_module(package)
from windops_backend.document_worker_health import isolated_runtime_preflight
with contextlib.redirect_stdout(sys.stderr):
    result = isolated_runtime_preflight(
        pathlib.Path(sys.argv[2]), sys.argv[3], pathlib.Path(sys.argv[4]))
print(json.dumps(result))
"""


def _configured(settings: Settings) -> bool:
    return bool(
        settings.document_parser_enabled
        and settings.document_parser_python
        and settings.document_parser_models_path
        and settings.document_parser_model_manifest_sha256
    )


def _lock_path() -> Path:
    checkout = Path(PACKAGE_PATH).parents[1] / "requirements.document-parser.txt"
    return checkout if checkout.is_file() else Path("/app/requirements.document-parser.txt")


def _release_valid(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None and set(value) != {"0"}


def isolated_runtime_preflight(models: Path, manifest: str, lock: Path) -> dict[str, Any]:
    """Executed exclusively in isolated Python: hash all weights and load the real pipeline."""
    from importlib import import_module

    from windops_backend.document_parse_identity import observed_parser_versions
    from windops_backend.document_parse_models import verify_model_bundle
    from windops_backend.structural_dependencies import verify_installed_lock

    closure = verify_installed_lock(lock)
    versions = observed_parser_versions()
    verify_model_bundle(models, manifest)
    base = import_module("docling.datamodel.base_models")
    options = import_module("docling.datamodel.pipeline_options")
    accelerator = import_module("docling.datamodel.accelerator_options")
    converters = import_module("docling.document_converter")
    pipeline = options.PdfPipelineOptions(
        artifacts_path=models,
        do_ocr=True,
        do_table_structure=True,
        enable_remote_services=False,
        allow_external_plugins=False,
        accelerator_options=accelerator.AcceleratorOptions(
            device=accelerator.AcceleratorDevice.CPU, num_threads=1
        ),
        ocr_options=options.RapidOcrOptions(
            lang=["ch"],
            backend="onnxruntime",
            rapidocr_params={
                "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            },
        ),
    )
    converter = converters.DocumentConverter(
        allowed_formats=[base.InputFormat.PDF],
        format_options={
            base.InputFormat.PDF: converters.PdfFormatOption(pipeline_options=pipeline)
        },
    )
    converter.initialize_pipeline(base.InputFormat.PDF)
    verify_model_bundle(models, manifest)
    return {
        "execution_code_sha256": parser_code_identity(),
        "library_versions": versions,
        "model_manifest_sha256": manifest,
        "requirements_lock_sha256": closure["requirements_lock_sha256"],
    }


async def probe_document_runtime(settings: Settings) -> dict[str, Any]:
    """No service credentials or network download settings enter the numerical child."""
    if not _configured(settings):
        raise RuntimeError("document_worker_unconfigured")
    runtime = await asyncio.to_thread(
        tempfile.TemporaryDirectory,
        prefix="openvigil-document-health-",
        dir=settings.document_parser_runtime_directory,
    )
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[Any]] = []
    try:
        models = await asyncio.to_thread(Path(str(settings.document_parser_models_path)).resolve)
        executable = str(settings.document_parser_python)
        if "/" in executable or "\\" in executable:
            executable = str(await asyncio.to_thread(Path(executable).resolve))
        process = await asyncio.create_subprocess_exec(
            executable,
            "-I",
            "-X",
            "utf8",
            "-c",
            PREFLIGHT_BOOTSTRAP,
            PACKAGE_PATH,
            str(models),
            str(settings.document_parser_model_manifest_sha256),
            str(_lock_path()),
            cwd=runtime.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
                },
                "HOME": runtime.name,
                "USERPROFILE": runtime.name,
                "HF_HOME": runtime.name,
                "XDG_CACHE_HOME": runtime.name,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "TOKENIZERS_PARALLELISM": "false",
            },
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("document_worker_probe_pipes")
        tasks = [
            asyncio.create_task(_bounded_read(process.stdout, MAX_RECORD_BYTES)),
            asyncio.create_task(_bounded_read(process.stderr, 64 * 1024)),
            asyncio.create_task(process.wait()),
        ]
        async with asyncio.timeout(PREFLIGHT_TIMEOUT_SECONDS):
            await asyncio.gather(*tasks)
        if process.returncode != 0:
            raise RuntimeError("document_worker_preflight_failed")
        result = json.loads(tasks[0].result())
        if not isinstance(result, dict):
            raise RuntimeError("document_worker_preflight_invalid")
        return result
    finally:
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(runtime.cleanup)


def _response(
    status: str, reasons: list[str], *, alive: bool = False, age: int | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "heartbeat_verified": alive,
        "runtime_ready": status == "ready",
        "observed_at": datetime.now(UTC).isoformat(),
        "last_heartbeat_age_seconds": age,
        "heartbeat_ttl_seconds": HEARTBEAT_TTL_SECONDS,
        "reason_codes": reasons,
    }


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if not isinstance(value, str):
        raise ValueError("invalid wire value")
    return value


async def read_document_worker_health(settings: Settings, redis_client: Any) -> dict[str, Any]:
    """Read only bounded TTL evidence; configuration alone never proves readiness."""
    if not _configured(settings):
        return _response("unconfigured", ["document_worker_unconfigured"])
    if redis_client is None:
        return _response("unavailable", ["document_worker_redis_unavailable"])
    try:
        async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
            values = await redis_client.eval(
                READ_SCRIPT,
                1,
                REGISTRY_KEY,
                HEARTBEAT_TTL_SECONDS,
                MAX_WORKERS,
                WORKER_KEY_PREFIX,
                MAX_RECORD_BYTES,
            )
        if not isinstance(values, (tuple, list)) or not 2 <= len(values) <= 2 + 2 * MAX_WORKERS:
            raise ValueError("invalid registry")
        now = float(_decode(values[0]))
        marker = _decode(values[1])
        if marker == "limit":
            return _response("unavailable", ["document_worker_limit_exceeded"])
        if marker != "ok" or len(values) % 2:
            raise ValueError("invalid registry")
        if len(values) == 2:
            return _response("offline", ["document_worker_heartbeat_missing"])
        expected_code = await asyncio.to_thread(parser_code_identity)
        expected_lock = await asyncio.to_thread(
            lambda: hashlib.sha256(_lock_path().read_bytes()).hexdigest()
        )
        statuses: set[str] = set()
        ages = []
        for index in range(2, len(values), 2):
            raw = _decode(values[index])
            if len(raw.encode()) > MAX_RECORD_BYTES:
                raise ValueError("oversized record")
            record = json.loads(raw)
            if not isinstance(record, dict) or record.get("contract") != CONTRACT:
                raise ValueError("invalid record")
            observed = record.get("observed_epoch")
            if isinstance(observed, bool) or not isinstance(observed, (int, float)):
                raise ValueError("invalid clock")
            age = now - observed
            ttl = int(_decode(values[index + 1]))
            if not 0 <= age < HEARTBEAT_TTL_SECONDS or not 0 < ttl <= HEARTBEAT_TTL_SECONDS * 1000:
                raise ValueError("invalid lease")
            ages.append(int(age))
            identity = record.get("identity")
            if not isinstance(identity, dict):
                raise ValueError("invalid identity")
            state = record.get("state")
            if state not in {"starting", "ready", "degraded"}:
                raise ValueError("invalid state")
            if (
                not _release_valid(settings.release_commit_sha)
                or record.get("release_commit_sha") != settings.release_commit_sha
                or (state == "ready" or identity)
                and (
                    identity.get("execution_code_sha256") != expected_code
                    or identity.get("library_versions") != DOCUMENT_PARSER_VERSIONS
                    or identity.get("model_manifest_sha256")
                    != settings.document_parser_model_manifest_sha256
                    or identity.get("requirements_lock_sha256") != expected_lock
                )
            ):
                statuses.add("version_mismatch")
            elif state == "ready" and (
                record.get("consumer_ready") is not True
                or type(record.get("preflight_age_seconds")) is not int
                or not 0 <= record["preflight_age_seconds"] + age <= PREFLIGHT_MAX_AGE_SECONDS
            ):
                statuses.add("degraded")
            else:
                statuses.add(state)
        status = next(
            value
            for value in ("version_mismatch", "degraded", "starting", "ready")
            if value in statuses
        )
        reasons = [] if status == "ready" else ["document_worker_" + status]
        return _response(status, reasons, alive=True, age=max(ages))
    except (TimeoutError, OSError, ValueError, TypeError, KeyError):
        return _response("unavailable", ["document_worker_evidence_unavailable"])
    except Exception:
        # Redis library errors must never return connection details or an old green result.
        logger.warning("Document worker health read failed")
        return _response("unavailable", ["document_worker_redis_unavailable"])


class DocumentWorkerHealth:
    """One monitor per worker process; import alone creates no connection or thread."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.worker: Any = None
        self.polls: dict[str, tuple[bool, float]] = {}
        self.iterations: dict[str, tuple[bool, float]] = {}
        self.poll_lock = threading.Lock()

    def observe_poll(self, queue: str, succeeded: bool) -> None:
        if queue in {"document-parse", "document-parse.DQ"}:
            with self.poll_lock:
                self.polls[queue] = succeeded, time.monotonic()

    def observe_iteration(self, queue: str, succeeded: bool) -> None:
        if queue in {"document-parse", "document-parse.DQ"}:
            with self.poll_lock:
                self.iterations[queue] = succeeded, time.monotonic()

    def consumer_ready(self) -> bool:
        if self.worker is None:
            return False
        now = time.monotonic()
        with self.poll_lock:
            polls = self.polls.copy()
            iterations = self.iterations.copy()
        # A prefetch-saturated main consumer may legitimately do no fetch during
        # a long parse. Its delayed consumer shares the broker/client and still
        # performs real Redis operations. Cache iteration alone is insufficient.
        if not any(ok and now - when < HEARTBEAT_TTL_SECONDS for ok, when in polls.values()):
            return False
        for queue in ("document-parse", "document-parse.DQ"):
            consumer = self.worker.consumers.get(queue)
            observed, when = iterations.get(queue, (False, 0.0))
            if not (
                observed
                and polls.get(queue, (False, 0.0))[0]
                and now - when < HEARTBEAT_TTL_SECONDS
                and consumer is not None
                and consumer.is_alive()
                and consumer.running
                and not consumer.paused
            ):
                return False
        return bool(
            any(
                item.is_alive() and item.running and not item.paused for item in self.worker.workers
            )
        )

    def after_worker_boot(self, broker: Any, worker: Any) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.worker = worker
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.monitor()), daemon=True, name="document-worker-health"
        )
        self.thread.start()

    def before_worker_shutdown(self, broker: Any, worker: Any) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=REDIS_TIMEOUT_SECONDS + 4)

    async def monitor(self) -> None:
        from redis.asyncio import Redis

        client: Redis = Redis.from_url(
            self.settings.redis_url,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            retry_on_timeout=False,
        )
        worker_id = uuid.uuid4().hex
        key = WORKER_KEY_PREFIX + worker_id
        probe: asyncio.Task[dict[str, Any]] | None = None
        identity: dict[str, Any] = {}
        verified_at = 0.0
        attempted = False
        failed = False
        next_probe = 0.0
        next_heartbeat = 0.0
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if probe is not None and probe.done():
                    try:
                        identity = probe.result()
                        verified_at = now
                        failed = False
                    except Exception:
                        failed = True
                        logger.warning("Document worker runtime preflight failed")
                    probe = None
                    next_probe = now + (
                        PREFLIGHT_RETRY_SECONDS if failed else PREFLIGHT_INTERVAL_SECONDS
                    )
                if probe is None and now >= next_probe:
                    probe = asyncio.create_task(probe_document_runtime(self.settings))
                if now >= next_heartbeat:
                    consumer = self.consumer_ready()
                    fresh = verified_at > 0 and now - verified_at <= PREFLIGHT_MAX_AGE_SECONDS
                    state = (
                        "ready"
                        if fresh and consumer and not failed and probe is None
                        else "degraded"
                    )
                    if not attempted and verified_at == 0 and not failed:
                        state = "starting"
                    record = {
                        "contract": CONTRACT,
                        "release_commit_sha": self.settings.release_commit_sha,
                        "identity": identity,
                        "state": state,
                        "consumer_ready": consumer,
                        "preflight_age_seconds": int(now - verified_at) if verified_at else None,
                    }
                    try:
                        async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                            await cast(
                                Awaitable[Any],
                                client.eval(
                                    PUBLISH_SCRIPT,
                                    2,
                                    REGISTRY_KEY,
                                    key,
                                    worker_id,
                                    json.dumps(record),
                                    str(HEARTBEAT_TTL_SECONDS),
                                ),
                            )
                    except Exception:
                        logger.warning("Document worker heartbeat publish failed")
                    attempted = verified_at > 0 or failed
                    next_heartbeat = now + HEARTBEAT_INTERVAL_SECONDS
                await asyncio.sleep(0.25)
        finally:
            if probe is not None:
                probe.cancel()
                outcomes = await asyncio.gather(probe, return_exceptions=True)
                if isinstance(outcomes[0], Exception):
                    logger.warning("Document worker runtime preflight failed during shutdown")
            try:
                async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                    await client.delete(key)
                    await client.zrem(REGISTRY_KEY, worker_id)
            except Exception:
                logger.warning("Document worker heartbeat cleanup failed; lease will expire")
            await client.aclose()


class ObservedDocumentConsumer:
    """Observe iteration liveness separately from the broker's real Redis operations."""

    def __init__(self, consumer: Any, health: DocumentWorkerHealth, queue: str) -> None:
        self.consumer, self.health, self.queue = consumer, health, queue

    def __iter__(self) -> ObservedDocumentConsumer:
        return self

    def __next__(self) -> Any:
        try:
            value = next(self.consumer)
        except Exception:
            self.health.observe_iteration(self.queue, False)
            raise
        self.health.observe_iteration(self.queue, True)
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self.consumer, name)
