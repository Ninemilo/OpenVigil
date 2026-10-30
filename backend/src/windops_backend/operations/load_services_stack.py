"""Fresh Compose project lifecycle, restricted to owned local dependencies.

No existing stack is reused. Every destructive operation rechecks project labels,
the frozen manifest, named volumes and mounts. CLI output stays in private files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # PyYAML ships no typing metadata.

from windops_backend.operations.load_services_commands import run_owned_command
from windops_backend.operations.load_services_processes import TrustedLaunch
from windops_backend.operations.load_services_resources import ContainerRole, ContainerSample
from windops_backend.operations.load_services_settings import ServicesEndpoints
from windops_backend.operations.load_software import ROOT, validate_run_directory

PORTS = {"postgres": 5432, "redis": 6379, "minio": 9000, "neo4j": 7687}
PROJECT_LABEL = "com.docker.compose.project"
_MAX_OUTPUT = 2 * 1024 * 1024
_ROLE_ORDER: tuple[ContainerRole, ...] = ("postgres", "redis", "minio", "neo4j")
_STATS_TIMEOUT = 5.0


def _container_identity(entry: dict[str, Any]) -> tuple[str, str, str, str, int]:
    """Freeze both immutable container and running-instance identity."""
    state = entry.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise StackError("STACK_CONTAINER_NOT_RUNNING")
    cid, image, created = entry.get("Id"), entry.get("Image"), entry.get("Created")
    started, pid = state.get("StartedAt"), state.get("Pid")
    if (
        not isinstance(cid, str)
        or re.fullmatch(r"[0-9a-f]{64}", cid) is None
        or not isinstance(image, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None
        or not isinstance(created, str)
        or not isinstance(started, str)
        or type(pid) is not int
        or pid <= 0
    ):
        raise StackError("STACK_CONTAINER_IDENTITY_INVALID")
    for value in (created, started):
        try:
            stamp = datetime.fromisoformat(value)
            if len(value) > 40 or stamp.utcoffset() is None or stamp.year < 2000:
                raise ValueError()
        except ValueError:
            raise StackError("STACK_CONTAINER_IDENTITY_INVALID") from None
    return cid, image, created, started, pid


def _memory_bytes(value: Any) -> int:
    # Docker CLI formats memory in human-readable units. These are converted
    # rounded display values, not exact raw engine counters. Keep that provenance.
    if not isinstance(value, str):
        raise StackError("STACK_CONTAINER_METRICS_INVALID")
    match = re.fullmatch(
        r"([0-9]{1,16}(?:\.[0-9]{1,9})?)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)", value
    )
    if match is None:
        raise StackError("STACK_CONTAINER_METRICS_INVALID")
    scales = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    count = int((Decimal(match[1]) * scales[match[2]]).to_integral_value(rounding=ROUND_CEILING))
    if count > 1024**5:
        raise StackError("STACK_CONTAINER_METRICS_INVALID")
    return count


def _memory_usage(value: Any) -> int:
    if not isinstance(value, str) or len(value) > 100 or value.count("/") != 1:
        raise StackError("STACK_CONTAINER_METRICS_INVALID")
    usage, limit = (_memory_bytes(part.strip()) for part in value.split("/"))
    if limit <= 0 or usage > limit:
        raise StackError("STACK_CONTAINER_METRICS_INVALID")
    return usage


def _cpu_percent(value: Any) -> float:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,9})?%", value) is None:
        raise StackError("STACK_CONTAINER_METRICS_INVALID")
    number = Decimal(value[:-1])
    if number > 1_000_000:
        raise StackError("STACK_CONTAINER_METRICS_INVALID")
    return float(number)


def compose_definition(password: str) -> dict[str, Any]:
    if re.fullmatch(r"[a-f0-9]{64}", password) is None:
        raise ValueError("invalid private stack credential")
    source = yaml.safe_load((ROOT / "backend/docker-compose.yml").read_text(encoding="utf-8"))
    if not isinstance(source, dict) or set(source) != {"services", "volumes"}:
        raise ValueError("unexpected dependency manifest")
    services = source["services"]
    if not isinstance(services, dict) or set(services) != {*PORTS, "minio-init"}:
        raise ValueError("unexpected dependency service set")
    del services["minio-init"]
    volumes = source["volumes"]
    if not isinstance(volumes, dict) or set(volumes) != {f"windops-{name}" for name in PORTS}:
        raise ValueError("unexpected dependency volumes")
    if any(value is not None for value in volumes.values()):
        raise ValueError("external or explicitly named volumes are forbidden")
    for name, service in services.items():
        if set(service) - {
            "image",
            "build",
            "environment",
            "ports",
            "healthcheck",
            "volumes",
            "command",
        }:
            raise ValueError("unexpected dependency configuration")
        if service["volumes"] != [
            f"windops-{name}:"
            + {
                "postgres": "/home/postgres/pgdata/data",
                "redis": "/data",
                "minio": "/data",
                "neo4j": "/data",
            }[name]
        ]:
            raise ValueError("only owned named data volumes are allowed")
        if name == "minio":
            if service.get("build") != {
                "context": ".",
                "dockerfile": "deploy/Dockerfile.minio",
                "target": "minio",
            }:
                raise ValueError("unexpected object storage build")
            service["build"]["context"] = str(ROOT / "backend")
        elif re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", service.get("image", "")) is None:
            raise ValueError("dependency images must be digest pinned")
        service["ports"] = [f"127.0.0.1::{PORTS[name]}"]
    services["postgres"]["environment"]["POSTGRES_PASSWORD"] = password
    services["minio"]["environment"]["MINIO_ROOT_PASSWORD"] = password
    services["neo4j"]["environment"]["NEO4J_AUTH"] = f"neo4j/{password}"
    services["neo4j"]["healthcheck"]["test"] = [
        "CMD-SHELL",
        'cypher-shell -u neo4j -p "$${NEO4J_AUTH#*/}" "RETURN 1" || exit 1',
    ]
    return source


class StackError(RuntimeError):
    """Only a fixed safe code reaches the public report."""


class OwnedComposeStack:
    def __init__(self, directory: Path, run_id: str, password: str) -> None:
        validate_run_directory(directory, run_id)
        private = directory / "private"
        if not private.is_dir() or private.is_symlink() or private.is_junction():
            raise ValueError("an owned private run directory is required")
        self.directory = directory
        self.run_id = run_id
        self.project = "openvigil-performance-" + run_id
        self.manifest = private / "compose.yml"
        definition = compose_definition(password)
        with self.manifest.open("x", encoding="utf-8") as stream:
            yaml.safe_dump(definition, stream, sort_keys=False)
        self.manifest_sha256 = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.docker = shutil.which("docker")
        self.context: str | None = None
        self.endpoint: str | None = None
        self.preflight_passed = False
        self.attempted_start = False
        self.command_count = 0
        self._cleaned = False
        self._cleanup_task: asyncio.Task[dict[str, Any]] | None = None
        self.containers: dict[str, str] = {}
        self._container_instances: dict[str, tuple[str, str, str, str, int]] = {}
        self._ready = False
        self.events: list[dict[str, Any]] = []
        self.container_metric_semantics = {
            "memory": "docker_cli_cache_adjusted_formatted_bytes",
            "cpu": "docker_cli_formatted_cpu_percent",
            "precision": "CLI-rounded display values converted to bytes; not raw engine counters",
        }

    def _check_manifest(self) -> None:
        validate_run_directory(self.directory, self.run_id)
        private = self.manifest.parent
        if private.is_symlink() or private.is_junction() or self.manifest.is_symlink():
            raise StackError("STACK_MANIFEST_CHANGED")
        if hashlib.sha256(self.manifest.read_bytes()).hexdigest() != self.manifest_sha256:
            raise StackError("STACK_MANIFEST_CHANGED")

    async def _command(self, args: tuple[str, ...], *, deadline_seconds: float = 30) -> str:
        """Fixed internal argv only; bounded bytes, deadline and process cleanup."""
        if self.docker is None:
            raise StackError("SERVICE_RUNTIME_UNAVAILABLE")
        self.command_count += 1
        if self.command_count > 3000:
            raise StackError("STACK_COMMAND_BUDGET_EXCEEDED")
        command_number = self.command_count
        private = self.directory / "private"
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("DOCKER_")
        }
        try:
            result = await run_owned_command(
                TrustedLaunch(
                    argv=(self.docker, *args),
                    cwd=private,
                    env=env,
                    private_log=private / f"stack-{command_number:04d}.log",
                ),
                workspace=self.directory,
                deadline_seconds=deadline_seconds,
                maximum_output_bytes=_MAX_OUTPUT,
            )
            if not result.cleanup_passed:
                self.events.append(
                    {"event": "command_cleanup_failed", "command_number": command_number}
                )
            if result.reason_code == "COMMAND_CANCELLED":
                raise asyncio.CancelledError
            if not result.cleanup_passed:
                raise StackError("STACK_COMMAND_CLEANUP_FAILED")
            if result.reason_code == "COMMAND_OUTPUT_LIMIT":
                raise StackError("STACK_OUTPUT_LIMIT_EXCEEDED")
            if result.reason_code == "COMMAND_ORPHANS":
                raise StackError("STACK_COMMAND_ORPHANS")
            if result.reason_code is not None or result.exit_code != 0:
                raise StackError("STACK_COMMAND_FAILED")
            return result.stdout.decode("utf-8", errors="strict").strip()
        except asyncio.CancelledError:
            raise
        except StackError:
            raise
        except Exception:
            raise StackError("STACK_COMMAND_FAILED") from None

    async def _docker(self, *args: str, deadline_seconds: float = 30) -> str:
        if self.endpoint is None:
            raise StackError("STACK_CONTEXT_UNVERIFIED")
        return await self._command(
            ("--host", self.endpoint, *args), deadline_seconds=deadline_seconds
        )

    async def _compose(self, *args: str, deadline_seconds: float = 30) -> str:
        self._check_manifest()
        return await self._docker(
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.manifest),
            *args,
            deadline_seconds=deadline_seconds,
        )

    async def preflight(self) -> None:
        context = await self._command(("context", "show"))
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context) is None:
            raise StackError("STACK_CONTEXT_INVALID")
        raw = json.loads(await self._command(("context", "inspect", context)))
        if not isinstance(raw, list) or len(raw) != 1:
            raise StackError("STACK_CONTEXT_INVALID")
        endpoint = raw[0].get("Endpoints", {}).get("docker", {}).get("Host", "")
        if not (endpoint.startswith("npipe:////./pipe/") or endpoint.startswith("unix:///")):
            raise StackError("STACK_CONTEXT_NOT_LOCAL")
        self.context = context
        # Freeze the validated transport, not a mutable Docker context name.
        self.endpoint = endpoint
        await self._docker("info", "--format", "{{json .ServerVersion}}")
        await self._docker("compose", "version", "--short")
        if any((await self._inventory()).values()):
            raise StackError("STACK_PROJECT_ALREADY_EXISTS")
        self.preflight_passed = True
        self.events.append({"event": "preflight", "status": "passed"})

    async def _inventory(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for kind in ("container", "volume", "network"):
            args = ("--all",) if kind == "container" else ()
            field = "{{.Name}}" if kind == "volume" else "{{.ID}}"
            text = await self._docker(
                kind,
                "ls",
                *args,
                "--filter",
                f"label={PROJECT_LABEL}={self.project}",
                "--format",
                field,
            )
            values = text.splitlines() if text else []
            if len(values) > 16 or any(
                re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", v) is None for v in values
            ):
                raise StackError("STACK_INVENTORY_INVALID")
            result[kind] = values
        return result

    async def inspect_owned(self, *, require_ports: bool = True) -> dict[str, Any]:
        inventory = await self._inventory()
        evidence: dict[str, Any] = {"containers": {}, "volumes": [], "networks": []}
        for kind, identifiers in inventory.items():
            if not identifiers:
                continue
            raw = json.loads(await self._docker(kind, "inspect", *identifiers))
            if not isinstance(raw, list) or len(raw) != len(identifiers):
                raise StackError("STACK_INSPECTION_INVALID")
            for entry in raw:
                labels = (entry.get("Config", {}) if kind == "container" else entry).get(
                    "Labels"
                ) or {}
                if labels.get(PROJECT_LABEL) != self.project:
                    raise StackError("STACK_OWNERSHIP_MISMATCH")
                if kind == "container":
                    service = labels.get("com.docker.compose.service")
                    if service not in PORTS or service in evidence["containers"]:
                        raise StackError("STACK_SERVICE_MISMATCH")
                    for mount in entry.get("Mounts", []):
                        if (
                            mount.get("Type") != "volume"
                            or mount.get("Name") != f"{self.project}_windops-{service}"
                        ):
                            raise StackError("STACK_MOUNT_MISMATCH")
                    bindings = entry.get("NetworkSettings", {}).get("Ports", {})
                    published = bindings.get(f"{PORTS[service]}/tcp")
                    if published is None and not require_ports:
                        published = []
                    if published and (
                        not isinstance(published, list)
                        or len(published) != 1
                        or published[0].get("HostIp") != "127.0.0.1"
                    ):
                        raise StackError("STACK_PORT_MISMATCH")
                    if not published and require_ports:
                        raise StackError("STACK_PORT_MISMATCH")
                    if any(
                        value for key, value in bindings.items() if key != f"{PORTS[service]}/tcp"
                    ):
                        raise StackError("STACK_EXTRA_PUBLISHED_PORT")
                    cid = entry.get("Id", "")
                    if re.fullmatch(r"[0-9a-f]{64}", cid) is None:
                        raise StackError("STACK_CONTAINER_ID_INVALID")
                    previous = self.containers.get(service)
                    if previous is not None and previous != cid:
                        raise StackError("STACK_CONTAINER_REPLACED")
                    self.containers[service] = cid
                    if entry.get("State", {}).get("Running") is True:
                        identity = _container_identity(entry)
                        self._container_instances.setdefault(service, identity)
                    evidence["containers"][service] = {
                        "id": cid,
                        "image_id": entry.get("Image"),
                        "port": int(published[0]["HostPort"]) if published else None,
                        "running": entry.get("State", {}).get("Running") is True,
                    }
                else:
                    name = entry.get("Name", "")
                    allowed = (
                        {f"{self.project}_windops-{v}" for v in PORTS}
                        if kind == "volume"
                        else {f"{self.project}_default"}
                    )
                    if name not in allowed:
                        raise StackError("STACK_RESOURCE_NAME_MISMATCH")
                    evidence[kind + "s"].append(name)
        return evidence

    async def start(self) -> tuple[ServicesEndpoints, dict[str, Any]]:
        if not self.preflight_passed or self.context is None or self.attempted_start:
            raise StackError("STACK_INVALID_START_STATE")
        self.attempted_start = True
        await self._compose(
            "up", "--detach", "--wait", "--wait-timeout", "180", deadline_seconds=240
        )
        evidence = await self.inspect_owned()
        if set(evidence["containers"]) != set(PORTS) or not all(
            v["running"] for v in evidence["containers"].values()
        ):
            raise StackError("STACK_NOT_READY")
        endpoints = ServicesEndpoints(
            **{name: value["port"] for name, value in evidence["containers"].items()}
        )
        self.events.append({"event": "started", "status": "passed"})
        self._ready = True
        return endpoints, evidence

    async def _inspect_stats_identities(self, cids: tuple[str, ...]) -> None:
        raw = json.loads(
            await self._docker("container", "inspect", *cids, deadline_seconds=_STATS_TIMEOUT)
        )
        if not isinstance(raw, list) or len(raw) != 4:
            raise StackError("STACK_CONTAINER_STATS_IDENTITY_MISMATCH")
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise StackError("STACK_CONTAINER_STATS_IDENTITY_MISMATCH")
            labels = entry.get("Config", {}).get("Labels") or {}
            role = labels.get("com.docker.compose.service")
            if (
                role not in _ROLE_ORDER
                or role in seen
                or labels.get(PROJECT_LABEL) != self.project
                or entry.get("Id") != self.containers[role]
                or _container_identity(entry) != self._container_instances[role]
            ):
                raise StackError("STACK_CONTAINER_STATS_IDENTITY_MISMATCH")
            seen.add(role)

    async def container_stats(self) -> tuple[ContainerSample, ...]:
        """Read only four frozen IDs; reject restart, missing data and stale identity.

        Docker's Linux CLI cache-adjusted MemUsage is a rounded display value;
        converting its units does not recover raw-byte precision. No RSS summing.
        """
        if (
            not self._ready
            or self._cleaned
            or set(self.containers) != set(_ROLE_ORDER)
            or set(self._container_instances) != set(_ROLE_ORDER)
        ):
            raise StackError("STACK_CONTAINER_STATS_UNVERIFIED")
        self._check_manifest()
        cids = tuple(self.containers[role] for role in _ROLE_ORDER)
        if len(set(cids)) != 4 or any(
            re.fullmatch(r"[0-9a-f]{64}", cid) is None or self._container_instances[role][0] != cid
            for role, cid in zip(_ROLE_ORDER, cids, strict=True)
        ):
            raise StackError("STACK_CONTAINER_STATS_IDENTITY_MISMATCH")
        try:
            async with asyncio.timeout(_STATS_TIMEOUT):
                await self._inspect_stats_identities(cids)
                text = await self._docker(
                    "stats",
                    "--no-stream",
                    "--no-trunc",
                    "--format",
                    "{{json .}}",
                    *cids,
                    deadline_seconds=_STATS_TIMEOUT,
                )
                observed_at = datetime.now(UTC)
                rows = text.splitlines()
                if len(rows) != 4:
                    raise StackError("STACK_CONTAINER_METRICS_MISSING")
                samples: dict[str, ContainerSample] = {}
                by_id = {self.containers[role]: role for role in _ROLE_ORDER}
                for line in rows:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise StackError("STACK_CONTAINER_METRICS_INVALID")
                    cid = row.get("ID")
                    if not isinstance(cid, str) or cid not in by_id or cid in samples:
                        raise StackError("STACK_CONTAINER_STATS_IDENTITY_MISMATCH")
                    samples[cid] = ContainerSample(
                        role=by_id[cid],
                        container_id=cid,
                        observed_at=observed_at,
                        running=True,
                        memory_working_set_bytes=_memory_usage(row.get("MemUsage")),
                        cpu_percent=_cpu_percent(row.get("CPUPerc")),
                    )
                await self._inspect_stats_identities(cids)
                return tuple(samples[cid] for cid in cids)
        except asyncio.CancelledError:
            raise
        except StackError:
            raise
        except TimeoutError:
            raise StackError("STACK_CONTAINER_STATS_TIMEOUT") from None
        except (KeyError, ValueError, TypeError, AttributeError):
            raise StackError("STACK_CONTAINER_METRICS_INVALID") from None

    async def _cleanup(self) -> dict[str, Any]:
        if self._cleaned:
            return {"passed": True, "status": "already_cleaned"}
        if not self.attempted_start:
            self._cleaned = True
            return {"passed": True, "status": "not_needed"}
        self._check_manifest()
        await self.inspect_owned(require_ports=False)
        await self._compose("down", "--volumes", "--timeout", "20", deadline_seconds=90)
        remaining = await self._inventory()
        passed = not any(remaining.values())
        self._cleaned = passed
        self.events.append({"event": "cleanup", "status": "passed" if passed else "failed"})
        return {"passed": passed, "status": "removed" if passed else "resources_remaining"}

    async def cleanup(self) -> dict[str, Any]:
        """Repeated cancellation never interrupts inspection/removal/verification."""
        if self._cleaned:
            return {"passed": True, "status": "already_cleaned"}
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        cancelled = False
        while not self._cleanup_task.done():
            try:
                await asyncio.shield(self._cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        result = self._cleanup_task.result()
        return {**result, **({"cancellation_observed": True} if cancelled else {})}
