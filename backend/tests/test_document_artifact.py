"""Image policy and probe-result validation; real OCR is verified separately."""

from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from test_container_artifact import COMMIT_SHA, IMAGE, POLICY_PATH, RELEASE_ID, SOURCE_URL, _inspect
from test_document_parse_contract import document_result
from windops_backend.document_parse_contract import DocumentParseResult
from windops_backend.document_parse_identity import DOCUMENT_PARSER_VERSIONS, parser_code_identity
from windops_backend.operations.container_artifact import (
    ContainerArtifactError,
    load_release_policy,
)
from windops_backend.operations.document_artifact import (
    DOCUMENT_COMMAND,
    DOCUMENT_ENVIRONMENT,
    verify_document_inspect,
)
from windops_backend.operations.document_smoke import (
    DocumentSmokeExpectation,
    validate_document_smoke_result,
)

DOCUMENT_IMAGE = "ghcr.io/windops/document@sha256:" + "d" * 64


def images():
    policy = load_release_policy(POLICY_PATH)
    api = _inspect(policy)
    api["RootFS"] = {"Layers": ["sha256:" + "1" * 64]}
    document = copy.deepcopy(api)
    document.update(RepoDigests=[DOCUMENT_IMAGE], Os="linux", Architecture="amd64")
    document["RootFS"]["Layers"].append("sha256:" + "2" * 64)
    document["Config"].update(
        Cmd=list(DOCUMENT_COMMAND),
        Healthcheck={"Test": ["NONE"]},
        Env=[f"{name}={value}" for name, value in DOCUMENT_ENVIRONMENT.items()],
    )
    document["Config"]["Labels"]["org.opencontainers.image.base.name"] = IMAGE
    return document, api, policy


def verify(document, api, policy):
    return verify_document_inspect(
        document,
        api,
        image=DOCUMENT_IMAGE,
        api_image=IMAGE,
        release_id=RELEASE_ID,
        commit_sha=COMMIT_SHA,
        source_url=SOURCE_URL,
        policy=policy,
    )


def test_document_runtime_extends_exact_api_with_its_own_queue_and_environment():
    identity = verify(*images())
    assert identity["image"] == DOCUMENT_IMAGE
    assert identity["api_image"] == IMAGE
    assert identity["commit_sha"] == COMMIT_SHA


@pytest.mark.parametrize(
    "violation",
    [
        "digest",
        "parent",
        "source",
        "revision",
        "release",
        "user",
        "queue",
        "threads",
        "entrypoint",
        "healthcheck",
        "python",
        "models",
        "thread_environment",
        "duplicate_env",
        "layers",
        "os",
        "architecture",
        "api_command",
    ],
)
def test_document_image_cannot_change_identity_privileges_or_parser_isolation(violation):
    document, api, policy = images()
    config = document["Config"]
    if violation == "digest":
        document["RepoDigests"] = [IMAGE]
    elif violation == "parent":
        config["Labels"]["org.opencontainers.image.base.name"] = "other@sha256:" + "e" * 64
    elif violation in {"source", "revision", "release"}:
        suffix = "version" if violation == "release" else violation
        config["Labels"]["org.opencontainers.image." + suffix] = "other"
    elif violation == "user":
        config["User"] = "0:0"
    elif violation == "queue":
        config["Cmd"][3] = "business"
    elif violation == "threads":
        config["Cmd"][-1] = "4"
    elif violation == "entrypoint":
        config["Entrypoint"] = ["sh", "-c"]
    elif violation == "healthcheck":
        config["Healthcheck"] = {"Test": ["CMD", "true"]}
    elif violation in {"python", "models", "thread_environment"}:
        key = {
            "python": "WINDOPS_DOCUMENT_PARSER_PYTHON",
            "models": "WINDOPS_DOCUMENT_PARSER_MODELS_PATH",
            "thread_environment": "OMP_NUM_THREADS",
        }[violation]
        config["Env"] = [entry for entry in config["Env"] if not entry.startswith(key + "=")]
        config["Env"].append(key + "=unapproved")
    elif violation == "duplicate_env":
        config["Env"].append(config["Env"][0])
    elif violation == "layers":
        document["RootFS"]["Layers"] = ["sha256:" + "2" * 64]
    elif violation == "os":
        document["Os"] = "windows"
    elif violation == "architecture":
        document["Architecture"] = "arm64"
    else:
        api["Config"]["Cmd"] = ["dramatiq"]
    with pytest.raises(ContainerArtifactError):
        verify(document, api, policy)


def probe_input():
    raw = document_result()
    raw["execution_code_sha256"] = parser_code_identity()
    raw["library_versions"] = dict(DOCUMENT_PARSER_VERSIONS)
    expectation = DocumentSmokeExpectation(
        schema_version="openvigil.document-smoke.v1",
        fixture_sha256="a" * 64,
        source_page_count=2,
        passage_count=1,
        table_cell_count=0,
        passage_pages=[2],
        required_text=["+123.45 kN"],
    )
    return raw, expectation


def test_ocr_probe_verifies_content_location_review_and_frozen_identity():
    raw, expected = probe_input()
    report = validate_document_smoke_result(
        DocumentParseResult.model_validate(raw), expected, "b" * 64
    )
    assert report["source_page_count"] == 2
    assert report["numeric_review_required"] is True
    assert len(report["result_sha256"]) == 64
    assert "body" not in report


@pytest.mark.parametrize(
    "violation",
    [
        "source",
        "models",
        "code",
        "versions",
        "pages",
        "passages",
        "tables",
        "location",
        "text",
        "review",
    ],
)
def test_ocr_probe_rejects_plausible_but_wrong_results(violation):
    raw, expected = probe_input()
    if violation in {"source", "models"}:
        raw["source_sha256" if violation == "source" else "model_manifest_sha256"] = "c" * 64
    elif violation == "code":
        raw["execution_code_sha256"]["document_parse_compute.py"] = "c" * 64
    elif violation == "versions":
        raw["library_versions"]["pypdf"] = "6.17.0"
    elif violation == "review":
        raw["passages"][0]["native_locator"]["requires_numeric_review"] = False
    else:
        field, value = {
            "pages": ("source_page_count", 3),
            "passages": ("passage_count", 2),
            "tables": ("table_cell_count", 1),
            "location": ("passage_pages", [1]),
            "text": ("required_text", ["-123.45 kN"]),
        }[violation]
        expected = expected.model_copy(update={field: value})
    with pytest.raises(ValueError if violation == "review" else RuntimeError):
        validate_document_smoke_result(DocumentParseResult.model_validate(raw), expected, "b" * 64)


def test_document_runtime_checks_survive_python_optimization():
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            "from windops_backend.operations.document_smoke import _require; "
            "_require(False, 'policy')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0 and "document runtime smoke failed: policy" in result.stderr
