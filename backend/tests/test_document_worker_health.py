"""Lease/identity failures cannot make configured OCR appear ready."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from windops_backend import document_worker_health as health
from windops_backend.config import Settings
from windops_backend.document_parse_identity import DOCUMENT_PARSER_VERSIONS, parser_code_identity


@pytest.fixture
def configured(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        document_parser_enabled=True,
        document_parser_python=sys.executable,
        document_parser_models_path=tmp_path / "models",
        document_parser_runtime_directory=tmp_path,
        document_parser_model_manifest_sha256="a" * 64,
        release_commit_sha="b" * 40,
    )


def identity(settings: Settings) -> dict[str, Any]:
    return {
        "execution_code_sha256": parser_code_identity(),
        "library_versions": DOCUMENT_PARSER_VERSIONS.copy(),
        "model_manifest_sha256": settings.document_parser_model_manifest_sha256,
        "requirements_lock_sha256": hashlib.sha256(health._lock_path().read_bytes()).hexdigest(),
    }


def record(settings: Settings, **overrides: Any) -> dict[str, Any]:
    return {
        "contract": health.CONTRACT,
        "release_commit_sha": settings.release_commit_sha,
        "identity": identity(settings),
        "state": "ready",
        "consumer_ready": True,
        "preflight_age_seconds": 0,
        "observed_epoch": 1000.0,
        **overrides,
    }


class WireRedis:
    """The Redis boundary is injected; these tests do not claim a running Redis service."""

    def __init__(
        self, records: list[dict[str, Any]], *, now: float = 1000, ttl: int = 45000
    ) -> None:
        self.values: list[Any] = [str(now).encode(), b"ok"]
        for item in records:
            self.values.extend([json.dumps(item).encode(), str(ttl).encode()])
        self.calls: list[Any] = []

    async def eval(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.values


async def test_configuration_and_absent_lease_never_prove_readiness(configured: Settings) -> None:
    unconfigured = await health.read_document_worker_health(Settings(_env_file=None), None)
    assert unconfigured["status"] == "unconfigured"
    unavailable = await health.read_document_worker_health(configured, None)
    assert unavailable["status"] == "unavailable"
    offline = await health.read_document_worker_health(configured, WireRedis([]))
    assert offline["status"] == "offline"
    for result in (unconfigured, unavailable, offline):
        assert result["heartbeat_verified"] is False
        assert result["runtime_ready"] is False
        assert result["last_heartbeat_age_seconds"] is None


async def test_fresh_identity_is_ready_and_public_result_contains_no_worker_data(
    configured: Settings,
) -> None:
    item = record(configured, host="secret-host", credentials="secret-token", task="private-task")
    redis = WireRedis([item], now=1003.5, ttl=41500)
    result = await health.read_document_worker_health(configured, redis)
    assert result["status"] == "ready"
    assert result["runtime_ready"] is result["heartbeat_verified"] is True
    assert result["last_heartbeat_age_seconds"] == 3
    assert result["reason_codes"] == []
    assert len(redis.calls) == 1
    assert redis.calls[0] == (
        health.READ_SCRIPT,
        1,
        health.REGISTRY_KEY,
        45,
        32,
        health.WORKER_KEY_PREFIX,
        16384,
    )
    assert set(result) == {
        "status",
        "runtime_ready",
        "heartbeat_verified",
        "observed_at",
        "last_heartbeat_age_seconds",
        "heartbeat_ttl_seconds",
        "reason_codes",
    }
    assert "secret" not in json.dumps(result)
    assert "private-task" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_commit_sha", "c" * 40),
        ("code", "c" * 64),
        ("versions", "0.0.0"),
        ("models", "c" * 64),
        ("lock", "not-a-hash"),
        ("lock", "c" * 64),
    ],
)
async def test_any_active_old_version_blocks_green_even_with_healthy_worker(
    configured: Settings,
    field: str,
    value: str,
) -> None:
    bad = record(configured)
    if field == "release_commit_sha":
        bad[field] = value
    elif field == "code":
        bad["identity"]["execution_code_sha256"]["document_parse_compute.py"] = value
    elif field == "versions":
        bad["identity"]["library_versions"]["rapidocr"] = value
    else:
        bad["identity"][
            "model_manifest_sha256" if field == "models" else "requirements_lock_sha256"
        ] = value
    result = await health.read_document_worker_health(
        configured, WireRedis([record(configured), bad])
    )
    assert result["status"] == "version_mismatch"
    assert result["heartbeat_verified"] is True
    assert result["runtime_ready"] is False


@pytest.mark.parametrize("sha", ["", "unknown", "0" * 40])
async def test_release_identity_is_required_even_when_worker_matches(
    configured: Settings, sha: str
) -> None:
    configured.release_commit_sha = sha
    result = await health.read_document_worker_health(configured, WireRedis([record(configured)]))
    assert result["status"] == "version_mismatch"


@pytest.mark.parametrize(
    "change",
    [
        {"consumer_ready": False},
        {"consumer_ready": 1},
        {"preflight_age_seconds": 361},
        {"preflight_age_seconds": -1},
        {"preflight_age_seconds": True},
        {"preflight_age_seconds": None},
        {"state": "degraded"},
    ],
)
async def test_expired_preflight_or_unavailable_consumer_blocks_ready(
    configured: Settings, change: dict[str, Any]
) -> None:
    result = await health.read_document_worker_health(
        configured, WireRedis([record(configured, **change)])
    )
    assert result["status"] == "degraded"
    assert result["heartbeat_verified"] is True
    assert result["runtime_ready"] is False


async def test_starting_and_recovery_are_distinct_from_liveness(configured: Settings) -> None:
    initial = record(configured, state="starting", identity={}, preflight_age_seconds=None)
    result = await health.read_document_worker_health(configured, WireRedis([initial]))
    assert result["status"] == "starting"
    assert result["heartbeat_verified"] is True and result["runtime_ready"] is False
    result = await health.read_document_worker_health(configured, WireRedis([]))
    assert result["status"] == "offline"
    result = await health.read_document_worker_health(configured, WireRedis([record(configured)]))
    assert result["status"] == "ready"


@pytest.mark.parametrize("now,ttl", [(1045, 1), (999, 45000), (1000, 0), (1000, -1), (1000, 45001)])
async def test_dead_or_invalid_lease_is_never_green(
    configured: Settings, now: float, ttl: int
) -> None:
    result = await health.read_document_worker_health(
        configured, WireRedis([record(configured)], now=now, ttl=ttl)
    )
    assert result["status"] == "unavailable"
    assert result["heartbeat_verified"] is False


@pytest.mark.parametrize(
    "values",
    [
        [],
        [b"1000"],
        [b"1000", b"invalid"],
        [b"1000", b"limit"],
        [b"1000", b"ok", b"{}"],
        [b"1000", b"ok", b"not-json", b"1"],
        [b"1000", b"ok", b"x" * 16385, b"1"],
        [b"1000", b"ok"] + [b"{}", b"1"] * 33,
    ],
)
async def test_invalid_or_excessive_wire_is_bounded_and_safe(
    configured: Settings, values: list[Any]
) -> None:
    redis = WireRedis([])
    redis.values = values
    result = await health.read_document_worker_health(configured, redis)
    assert result["status"] == "unavailable"
    assert result["runtime_ready"] is False


async def test_redis_failures_and_timeouts_do_not_return_cached_success(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenRedis:
        async def eval(self, *args: Any) -> Any:
            raise RuntimeError("password=do-not-return")

    class HungRedis:
        async def eval(self, *args: Any) -> Any:
            await asyncio.sleep(20)

    monkeypatch.setattr(health, "REDIS_TIMEOUT_SECONDS", 0.01)
    for client in (BrokenRedis(), HungRedis()):
        result = await health.read_document_worker_health(configured, client)
        assert result["status"] == "unavailable"
        assert "password" not in json.dumps(result)


def test_consumer_poll_failure_pause_thread_death_and_recovery(configured: Settings) -> None:
    middleware = health.DocumentWorkerHealth(configured)
    alive = True
    consumer = SimpleNamespace(is_alive=lambda: alive, running=True, paused=False)
    worker = SimpleNamespace(is_alive=lambda: alive, running=True, paused=False)
    middleware.worker = SimpleNamespace(
        consumers={"document-parse": consumer, "document-parse.DQ": consumer}, workers=[worker]
    )
    assert not middleware.consumer_ready()
    middleware.observe_poll("document-parse", True)
    middleware.observe_iteration("document-parse", True)
    assert not middleware.consumer_ready()
    middleware.observe_poll("document-parse.DQ", True)
    middleware.observe_iteration("document-parse.DQ", True)
    assert middleware.consumer_ready()
    middleware.observe_poll("document-parse", False)
    assert not middleware.consumer_ready()
    middleware.observe_poll("document-parse", True)
    consumer.paused = True
    assert not middleware.consumer_ready()
    consumer.paused = False
    alive = False
    assert not middleware.consumer_ready()
    alive = True
    assert middleware.consumer_ready()
    middleware.polls["document-parse"] = (True, 0)
    assert middleware.consumer_ready()  # Busy main queue; delayed queue proves broker connectivity.
    middleware.polls["document-parse.DQ"] = (True, 0)
    assert not middleware.consumer_ready()


def test_consumer_proxy_preserves_messages_ack_and_exceptions(configured: Settings) -> None:
    class Consumer:
        error = False
        acked: list[str] = []

        def __next__(self) -> str:
            if self.error:
                raise ConnectionError("private connection detail")
            return "message"

        def ack(self, value: str) -> None:
            self.acked.append(value)

    observer = health.DocumentWorkerHealth(configured)
    wrapped = Consumer()
    consumer = health.ObservedDocumentConsumer(wrapped, observer, "document-parse")
    assert next(iter(consumer)) == "message"
    assert observer.iterations["document-parse"][0] is True
    assert observer.polls == {}  # Iterator/cache activity never invents a successful Redis call.
    consumer.ack("message")
    assert wrapped.acked == ["message"]
    wrapped.error = True
    with pytest.raises(ConnectionError):
        next(consumer)
    assert observer.iterations["document-parse"][0] is False


def test_actual_redis_consumer_prefetch_saturation_does_not_refresh_redis_evidence(
    configured: Settings,
) -> None:
    from dramatiq.brokers.redis import _RedisConsumer

    observer = health.DocumentWorkerHealth(configured)

    def forbidden_fetch(*args: Any) -> Any:
        raise AssertionError("saturated consumer must not call Redis fetch")

    consumer = _RedisConsumer(
        SimpleNamespace(do_fetch=forbidden_fetch), "document-parse", prefetch=1, timeout=1
    )
    consumer.queued_message_ids.add("already-processing")
    wrapped = health.ObservedDocumentConsumer(consumer, observer, "document-parse")
    assert next(wrapped) is None
    assert observer.iterations["document-parse"][0] is True
    assert observer.polls == {}
    assert not observer.consumer_ready()


def test_actual_dramatiq_consumer_threads_pause_resume_and_shutdown(configured: Settings) -> None:
    import dramatiq
    from dramatiq.brokers.stub import StubBroker

    observer = health.DocumentWorkerHealth(configured)

    class ObservedBroker(StubBroker):
        def consume(self, queue_name: str, prefetch: int = 1, timeout: int = 5000) -> Any:
            underlying = super().consume(queue_name, prefetch=prefetch, timeout=timeout)

            class Transport:
                def __next__(self) -> Any:
                    value = next(underlying)
                    # This test exercises local queue transport, not Redis network I/O.
                    observer.observe_poll(queue_name, True)
                    return value

                def __getattr__(self, name: str) -> Any:
                    return getattr(underlying, name)

            return health.ObservedDocumentConsumer(Transport(), observer, queue_name)

    broker = ObservedBroker()
    handled = threading.Event()

    @dramatiq.actor(broker=broker, queue_name="document-parse")
    def receive() -> None:
        handled.set()

    worker = dramatiq.Worker(broker, queues={"document-parse"}, worker_threads=1, worker_timeout=20)
    observer.worker = worker
    try:
        worker.start()
        receive.send()
        assert handled.wait(2)
        deadline = time.monotonic() + 2
        while not observer.consumer_ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observer.consumer_ready()
        worker.pause()
        assert not observer.consumer_ready()
        worker.resume()
        deadline = time.monotonic() + 2
        while not observer.consumer_ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observer.consumer_ready()
    finally:
        worker.stop(timeout=2000)
        broker.close()
    assert not observer.consumer_ready()


async def test_monitor_discards_old_green_on_preflight_failure_and_recovers(
    configured: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redis.asyncio import Redis

    monitor = health.DocumentWorkerHealth(configured)
    monkeypatch.setattr(monitor, "consumer_ready", lambda: True)
    monkeypatch.setattr(health, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(health, "PREFLIGHT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(health, "PREFLIGHT_RETRY_SECONDS", 0.01)
    calls = 0

    async def probe(settings: Settings) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("secret model path")
        return identity(settings)

    states: list[str] = []
    removed: list[str] = []

    class Client:
        async def eval(self, *args: Any) -> int:
            item = json.loads(args[5])
            states.append(item["state"])
            if states.count("ready") == 2:
                monitor.stop_event.set()
            return 1

        async def delete(self, key: str) -> None:
            removed.append(key)

        async def zrem(self, key: str, member: str) -> None:
            removed.append(key)

        async def aclose(self) -> None:
            removed.append("closed")

    monkeypatch.setattr(health, "probe_document_runtime", probe)
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: Client())
    async with asyncio.timeout(5):
        await monitor.monitor()
    assert states[0] == "starting"
    first_ready = states.index("ready")
    second_ready = len(states) - 1
    assert "degraded" in states[first_ready + 1 : second_ready]
    assert calls >= 3
    assert len(removed) == 3 and removed[-1] == "closed"


async def test_real_subprocess_timeout_kills_child_and_removes_owned_scratch(
    configured: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_create = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []

    async def capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await actual_create(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setattr(health, "PREFLIGHT_BOOTSTRAP", "import time; time.sleep(20)")
    monkeypatch.setattr(health, "PREFLIGHT_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(TimeoutError):
        await health.probe_document_runtime(configured)
    assert len(children) == 1 and children[0].returncode is not None
    assert configured.document_parser_runtime_directory is not None
    assert list(configured.document_parser_runtime_directory.iterdir()) == []


async def test_failed_probe_at_shutdown_still_removes_lease_and_closes_client(
    configured: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redis.asyncio import Redis

    monitor = health.DocumentWorkerHealth(configured)
    removed: list[str] = []

    class Client:
        async def eval(self, *args: Any) -> int:
            return 1

        async def delete(self, key: str) -> None:
            removed.append("lease")

        async def zrem(self, key: str, member: str) -> None:
            removed.append("registry")

        async def aclose(self) -> None:
            removed.append("closed")

    async def failing_probe(settings: Settings) -> dict[str, Any]:
        monitor.stop_event.set()
        raise RuntimeError("private runtime error")

    monkeypatch.setattr(health, "probe_document_runtime", failing_probe)
    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: Client())
    async with asyncio.timeout(2):
        await monitor.monitor()
    assert removed == ["lease", "registry", "closed"]


async def test_actual_preflight_rejects_wrong_runtime_and_cleans_scratch(
    configured: Settings,
) -> None:
    # This interpreter deliberately lacks the independently frozen OCR environment.
    with pytest.raises(RuntimeError, match="document_worker_preflight_failed"):
        await health.probe_document_runtime(configured)
    assert configured.document_parser_runtime_directory is not None
    assert list(configured.document_parser_runtime_directory.iterdir()) == []


def test_api_import_has_no_worker_or_numerical_import_side_effects() -> None:
    script = """
import sys
import windops_backend.document_worker_health
for name in sys.modules:
    assert not name.startswith(('docling', 'rapidocr', 'torch', 'dramatiq.brokers'))
assert 'windops_backend.document_parse_tasks' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=15)


def test_worker_entrypoint_registers_health_middleware_without_starting_thread() -> None:
    script = """
import dramatiq
import windops_backend.document_parse_tasks as tasks
broker = dramatiq.get_broker()
assert isinstance(broker, tasks.DocumentRedisBroker)
middleware = broker.document_health
assert middleware in broker.middleware
assert middleware.thread is None
assert tasks.parse_knowledge_document.queue_name == 'document-parse'
from unittest.mock import patch
from dramatiq.brokers.redis import RedisBroker
def successful(queue, *args):
    return ['wire-message']
with patch.object(RedisBroker, '_dispatch', return_value=successful):
    assert broker.do_fetch('document-parse', 1) == ['wire-message']
assert middleware.polls['document-parse'][0] is True
def failed(queue, *args):
    raise ConnectionError('private connection error')
with patch.object(RedisBroker, '_dispatch', return_value=failed):
    try:
        broker.do_fetch('document-parse', 1)
    except ConnectionError:
        pass
    else:
        raise AssertionError('broker error swallowed')
assert middleware.polls['document-parse'][0] is False
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=15)
