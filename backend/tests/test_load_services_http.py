"""Real owned TLS sockets with synthetic control responses, never service evidence."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from windops_backend.operations.load_runner import PrivateUser
from windops_backend.operations.load_services_http import ObservationError, ServicesHttp
from windops_backend.operations.load_services_server import _certificate


@asynccontextmanager
async def tls_server(tmp_path):
    _certificate(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    client_context = ssl.create_default_context(cafile=str(tmp_path / "cert.pem"))
    handlers = set()
    requests = []

    async def serve(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            raw = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            target = raw.split("\r\n")[0].split(" ")[1]
            requests.append(target)
            case = parse_qs(urlsplit(target).query).get("case", ["ok"])[0]
            if case == "slow":
                await asyncio.sleep(2)
            status = 401 if case == "denied" else 200
            body = json.dumps({"state": case, "private": "do-not-report-body"}).encode()
            if case == "large":
                body += b" " * 2000
            writer.write(
                (
                    f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # Expected transport closure in timeout/cancellation tests.
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context)
    try:
        yield f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}", client_context, requests
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)


def observer(origin, context, **kwargs):
    return ServicesHttp(
        origin=origin,
        verify=context,
        users=[PrivateUser("a1", {"Authorization": "Bearer private-control-token"})],
        maximum_requests=2,
        **kwargs,
    )


async def test_observations_and_denials_have_separate_safe_ledger(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with tls_server(tmp_path) as (origin, context, requests):
        client = observer(origin, context)
        for case in ("ok", "denied"):
            await client.get(
                route_id="health",
                path="/api/v1/knowledge/parser-capabilities",
                user_id="a1",
                query={"case": case, "private": "query-secret"},
                expected_status=401 if case == "denied" else 200,
                validator=lambda value, expected=case: (
                    isinstance(value, dict) and value["state"] == expected
                ),
            )
        with pytest.raises(ObservationError, match="REQUEST_BUDGET"):
            await client.get(
                route_id="health", path="/api/v1/readyz", user_id="a1", validator=lambda _: True
            )
    report = client.report()
    assert len(requests) == 2 and report["requests_used"] == 2
    assert report["kind"] == "control_or_observation_not_business_throughput"
    assert all(s["outcome"] == "success" for s in report["samples"])
    public = json.dumps(report) + caplog.text
    assert "private-control-token" not in public and "query-secret" not in public
    assert "do-not-report-body" not in public


@pytest.mark.parametrize(
    "case,code", [("large", "RESPONSE_BUDGET"), ("slow", "TIMEOUT"), ("ok", "SEMANTIC_FAILURE")]
)
async def test_response_limits_timeout_and_semantic_failure_preserved(tmp_path, case, code) -> None:
    async with tls_server(tmp_path) as (origin, context, _):
        client = observer(
            origin, context, maximum_response_bytes=1024, request_timeout_seconds=0.15
        )
        with pytest.raises(ObservationError, match=code):
            await client.get(
                route_id="health",
                path="/api/v1/readyz",
                user_id="a1",
                query={"case": case},
                validator=lambda _: False,
            )
    assert client.samples[0].outcome in {"failure", "timeout"} and client.cleanup_passed


async def test_repeated_cancellation_during_client_cleanup_is_bounded(
    tmp_path, monkeypatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = httpx.AsyncClient.aclose

    async def close(client):
        await original(client)
        entered.set()
        await release.wait()

    monkeypatch.setattr(httpx.AsyncClient, "aclose", close)
    async with tls_server(tmp_path) as (origin, context, _):
        client = observer(origin, context)
        task = asyncio.create_task(
            client.get(
                route_id="ready", path="/api/v1/readyz", user_id="a1", validator=lambda _: True
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert client.cleanup_passed and client.samples[0].outcome == "cancelled"


def test_tls_verification_cannot_be_disabled() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verified services TLS"):
        observer("https://127.0.0.1:1234", context)
