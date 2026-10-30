"""Verify a digest-addressed OCR image and its offline isolated runtime."""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from windops_backend.document_parse_identity import DOCUMENT_PARSER_VERSIONS, parser_code_identity
from windops_backend.document_parse_models import verify_model_bundle
from windops_backend.operations.container_artifact import (
    ContainerArtifactError,
    _release_digest,
    load_release_policy,
    verify_image_inspect,
)
from windops_backend.operations.document_smoke import DocumentSmokeExpectation
from windops_backend.operations.report_io import sha256_file, write_atomic_json

DOCUMENT_COMMAND = [
    "dramatiq",
    "windops_backend.document_parse_tasks",
    "--queues",
    "document-parse",
    "--processes",
    "1",
    "--threads",
    "1",
]
DOCUMENT_ENVIRONMENT = {
    "WINDOPS_DOCUMENT_PARSER_PYTHON": "/opt/document-parser/bin/python",
    "WINDOPS_DOCUMENT_PARSER_MODELS_PATH": "/models",
    "WINDOPS_DOCUMENT_PARSER_RUNTIME_DIRECTORY": "/tmp",  # nosec B108
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
}


def verify_document_inspect(
    inspected: dict[str, Any],
    api_inspected: dict[str, Any],
    *,
    image: str,
    api_image: str,
    release_id: str,
    commit_sha: str,
    source_url: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    api = verify_image_inspect(
        api_inspected,
        image=api_image,
        release_id=release_id,
        commit_sha=commit_sha,
        source_url=source_url,
        policy=policy,
    )
    digest = _release_digest(image)
    if digest == api["image_digest"] or image not in inspected.get("RepoDigests", []):
        raise ContainerArtifactError("document image must have its own verified digest")
    if inspected.get("Os") != "linux" or inspected.get("Architecture") != "amd64":
        raise ContainerArtifactError("document image requires linux/amd64")
    config = inspected.get("Config", {})
    if (
        config.get("User") != "10001:10001"
        or config.get("Cmd") != DOCUMENT_COMMAND
        or config.get("Entrypoint") not in (None, [])
        or config.get("Healthcheck", {}).get("Test") != ["NONE"]
    ):
        raise ContainerArtifactError("document image requires the isolated non-root queue command")
    expected_labels = {
        "org.opencontainers.image.version": release_id,
        "org.opencontainers.image.revision": commit_sha,
        "org.opencontainers.image.source": source_url,
        "org.opencontainers.image.base.name": api_image,
        "org.windops.postgresql-client.image": policy["postgres_client_image"],
        "org.windops.postgresql-client.major": str(policy["postgres_client_major"]),
    }
    if any(config.get("Labels", {}).get(key) != value for key, value in expected_labels.items()):
        raise ContainerArtifactError("document image labels differ from the approved API parent")
    environment = config.get("Env", [])
    for name, value in DOCUMENT_ENVIRONMENT.items():
        matching = [entry for entry in environment if entry.startswith(name + "=")]
        if matching != [f"{name}={value}"]:
            raise ContainerArtifactError(
                "document image parser environment is not isolated and bounded"
            )
    api_layers = api_inspected.get("RootFS", {}).get("Layers", [])
    layers = inspected.get("RootFS", {}).get("Layers", [])
    if not api_layers or len(layers) <= len(api_layers) or layers[: len(api_layers)] != api_layers:
        raise ContainerArtifactError("document layers do not extend the exact API image")
    return {
        "image": image,
        "image_digest": digest,
        "api_image": api_image,
        "api_image_digest": api["image_digest"],
        "release_id": release_id,
        "commit_sha": commit_sha,
        "source_url": source_url,
        "platform": "linux/amd64",
    }


def _run(command: list[str]) -> str:
    return subprocess.run(  # nosec B603
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    ).stdout


def verify_document_artifact(
    *,
    image: str,
    api_image: str,
    release_id: str,
    commit_sha: str,
    source_url: str,
    policy_path: Path,
    lock_path: Path,
    models: Path,
    fixture: Path,
    expectations: Path,
    manifest_sha256: str,
) -> dict[str, Any]:
    verify_model_bundle(models, manifest_sha256)
    if expectations.stat().st_size > 64 * 1024:
        raise ContainerArtifactError("document probe expectations exceed their size limit")
    expected = DocumentSmokeExpectation.model_validate_json(expectations.read_bytes())
    if sha256_file(fixture) != expected.fixture_sha256:
        raise ContainerArtifactError("document probe fixture does not match approved expectations")
    for reference in (api_image, image):
        _release_digest(reference)
        _run(["docker", "pull", reference])
    api = json.loads(_run(["docker", "image", "inspect", api_image]))[0]
    document = json.loads(_run(["docker", "image", "inspect", image]))[0]
    identity = verify_document_inspect(
        document,
        api,
        image=image,
        api_image=api_image,
        release_id=release_id,
        commit_sha=commit_sha,
        source_url=source_url,
        policy=load_release_policy(policy_path),
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "3g",
        "--cpus",
        "2",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m,uid=10001,gid=10001",  # nosec B108
    ]
    for source, target in (
        (models, "/models"),
        (fixture, "/probe/source.pdf"),
        (expectations, "/probe/expected.json"),
    ):
        command.extend(["--mount", f"type=bind,source={source.resolve()},target={target},readonly"])
    command.extend(
        [
            "--entrypoint",
            "python",
            image,
            "-m",
            "windops_backend.operations.document_smoke",
            "--models",
            "/models",
            "--fixture",
            "/probe/source.pdf",
            "--expectations",
            "/probe/expected.json",
            "--model-manifest-sha256",
            manifest_sha256,
        ]
    )
    runtime = json.loads(_run(command).splitlines()[-1])
    if runtime["dependency_closure"]["requirements_lock_sha256"] != sha256_file(lock_path):
        raise ContainerArtifactError("document runtime lock differs from the build source")
    if runtime["expectations_sha256"] != sha256_file(expectations):
        raise ContainerArtifactError("document runtime used different probe expectations")
    if (
        runtime["parse"]["execution_code_sha256"] != parser_code_identity()
        or runtime["parse"]["library_versions"] != DOCUMENT_PARSER_VERSIONS
        or runtime["parse"]["model_manifest_sha256"] != manifest_sha256
        or runtime["parse"]["source_sha256"] != expected.fixture_sha256
    ):
        raise ContainerArtifactError("document runtime differs from the approved code or inputs")
    verify_model_bundle(models, manifest_sha256)
    return {
        "format_version": 1,
        "gate": "document_container",
        "status": "passed",
        "identity": identity,
        "runtime": runtime,
        "api_inspect": api,
        "document_inspect": document,
        "policy_sha256": sha256_file(policy_path),
        "field_qualification": "unverified",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "image",
        "api-image",
        "release-id",
        "commit-sha",
        "source-url",
        "model-manifest-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    for name in ("policy", "lock", "models", "fixture", "expectations", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists() or args.report.is_symlink():
        raise ContainerArtifactError("document artifact report already exists or is unsafe")
    report = verify_document_artifact(
        image=args.image,
        api_image=args.api_image,
        release_id=args.release_id,
        commit_sha=args.commit_sha,
        source_url=args.source_url,
        policy_path=args.policy,
        lock_path=args.lock,
        models=args.models,
        fixture=args.fixture,
        expectations=args.expectations,
        manifest_sha256=args.model_manifest_sha256,
    )
    write_atomic_json(args.report, report, symlink_error=ContainerArtifactError("unsafe report"))
    print(json.dumps({"status": report["status"], "identity": report["identity"]}))


if __name__ == "__main__":
    main()
