"""Unit gate contracts; these fixtures never establish a real release qualification."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from test_document_parse_contract import document_result
from test_hybrid_release_gate import API, COMMIT, RELEASE, STRUCTURAL
from windops_backend.document_parse_identity import DOCUMENT_PARSER_VERSIONS, parser_code_identity
from windops_backend.operations.document_identity import document_bundle_id
from windops_backend.operations.document_release_gate import (
    DOCUMENT_CHECKS,
    PARSED_RESULT,
    verify_document_acceptance,
    verify_document_release,
)
from windops_backend.operations.document_smoke import DocumentSmokeExpectation
from windops_backend.operations.hybrid_identity import hybrid_bundle_id
from windops_backend.operations.report_io import sha256_file

DOCUMENT = "sha256:" + "d" * 64
MODELS = "b" * 64


def _identity():
    bundle = document_bundle_id(RELEASE, COMMIT, API, STRUCTURAL, DOCUMENT, MODELS)
    return {
        "release_id": RELEASE,
        "commit_sha": COMMIT,
        "api_image_digest": API,
        "structural_image_digest": STRUCTURAL,
        "image_digest": DOCUMENT,
        "model_manifest_sha256": MODELS,
        "bundle_id": bundle,
        "evidence_set_id": bundle,
        "hybrid_bundle_id": hybrid_bundle_id(RELEASE, COMMIT, API, STRUCTURAL),
    }


def _acceptance(root: Path):
    junit = root / "artifacts/document/cases.xml"
    junit.parent.mkdir(parents=True)
    junit.write_text('<testsuites><testsuite><testcase name="unit-only"/></testsuite></testsuites>')
    result = document_result()
    result.update(
        execution_code_sha256=parser_code_identity(), library_versions=DOCUMENT_PARSER_VERSIONS
    )
    parsed = root / PARSED_RESULT
    parsed.write_text(json.dumps(result))
    expected = DocumentSmokeExpectation(
        schema_version="openvigil.document-smoke.v1",
        fixture_sha256=result["source_sha256"],
        source_page_count=2,
        passage_count=1,
        table_cell_count=0,
        passage_pages=[2],
        required_text=["+123.45 kN"],
    )
    now = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    report = {
        "format_version": 2,
        "gate": "document_runtime",
        "status": "passed",
        "release": _identity(),
        "started_at": now,
        "completed_at": now,
        "target": "unit-contract-only",
        "tool": {"name": "unit-contract", "version": "1"},
        "approval": {"approved_by": "unit-reviewer", "approval_reference": "UNIT-ONLY"},
        "checks": [
            {
                "id": check,
                "status": "passed",
                "summary": "Unit schema fixture only",
                "artifact_paths": ["artifacts/document/cases.xml", PARSED_RESULT],
            }
            for check in sorted(DOCUMENT_CHECKS)
        ],
        "artifacts": [
            {
                "path": "artifacts/document/cases.xml",
                "sha256": sha256_file(junit),
                "media_type": "application/xml",
            },
            {
                "path": PARSED_RESULT,
                "sha256": sha256_file(parsed),
                "media_type": "application/json",
            },
        ],
    }
    path = root / "reports/document_runtime.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report))
    return expected, report, path, junit, parsed


def test_document_acceptance_checks_content_without_qualifying_release(tmp_path):
    expected, report, _, _, _ = _acceptance(tmp_path)
    assert verify_document_acceptance(tmp_path, _identity(), expected) == report
    assert not (tmp_path / "document-release-qualification.json").exists()


@pytest.mark.parametrize("field", list(_identity()))
def test_document_acceptance_rejects_each_drifted_identity_field(tmp_path, field):
    expected, report, path, _, _ = _acceptance(tmp_path)
    report["release"][field] = "changed"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        verify_document_acceptance(tmp_path, _identity(), expected)


@pytest.mark.parametrize(
    "violation",
    [
        "missing_check",
        "duplicate_check",
        "missing_result",
        "tampered",
        "undeclared",
        "outside",
        "failure",
        "error",
        "skipped",
        "counter",
        "doctype",
        "empty_junit",
        "wrong_root",
        "future",
        "stale",
        "duration",
        "failed_report",
        "model",
        "code",
        "version",
        "numeric",
        "duplicate_artifact",
        "no_junit",
    ],
)
def test_document_acceptance_rejects_incomplete_failed_or_stale_evidence(tmp_path, violation):
    expected, report, path, junit, parsed = _acceptance(tmp_path)
    if violation == "missing_check":
        report["checks"].pop()
    elif violation == "duplicate_check":
        report["checks"].append(copy.deepcopy(report["checks"][0]))
    elif violation == "missing_result":
        parsed.unlink()
    elif violation == "tampered":
        junit.write_text("changed")
    elif violation == "undeclared":
        report["checks"][0]["artifact_paths"].append("artifacts/unknown.json")
    elif violation == "outside":
        report["artifacts"][0]["path"] = "../escape.xml"
    elif violation in {
        "failure",
        "error",
        "skipped",
        "counter",
        "doctype",
        "empty_junit",
        "wrong_root",
    }:
        data = '<testsuite><testcase name="unit">' + f"<{violation}/>" + "</testcase></testsuite>"
        data = {
            "counter": '<testsuite failures="1"><testcase name="unit"/></testsuite>',
            "doctype": '<!DOCTYPE x><testsuite><testcase name="unit"/></testsuite>',
            "empty_junit": "<testsuite/>",
            "wrong_root": '<evidence><testcase name="unit"/></evidence>',
        }.get(violation, data)
        junit.write_text(data)
        report["artifacts"][0]["sha256"] = sha256_file(junit)
    elif violation in {"future", "stale", "duration"}:
        offset = 1 if violation == "future" else -4
        report["started_at"] = (datetime.now(UTC) + timedelta(days=offset)).isoformat()
        if violation != "duration":
            report["completed_at"] = report["started_at"]
    elif violation == "failed_report":
        report["status"] = "failed"
    elif violation in {"model", "code", "version", "numeric"}:
        result = json.loads(parsed.read_bytes())
        if violation == "model":
            result["model_manifest_sha256"] = "f" * 64
        elif violation == "code":
            result["execution_code_sha256"] = {}
        elif violation == "version":
            result["library_versions"]["pypdf"] = "6.17.0"
        else:
            result["passages"][0]["native_locator"]["requires_numeric_review"] = False
        parsed.write_text(json.dumps(result))
        report["artifacts"][1]["sha256"] = sha256_file(parsed)
    elif violation == "duplicate_artifact":
        report["artifacts"].append(copy.deepcopy(report["artifacts"][0]))
    else:
        report["checks"][0]["artifact_paths"] = [PARSED_RESULT]
    path.write_text(json.dumps(report))
    with pytest.raises((ValueError, RuntimeError, OSError)):
        verify_document_acceptance(tmp_path, _identity(), expected)


def test_document_evidence_alone_cannot_qualify_missing_api_and_structural_gates(tmp_path):
    expected, _, _, _, _ = _acceptance(tmp_path)
    expectations = tmp_path / "expected.json"
    expectations.write_text(expected.model_dump_json())
    with pytest.raises((ValueError, OSError)):
        verify_document_release(
            evidence_dir=tmp_path,
            release_id=RELEASE,
            commit_sha=COMMIT,
            api_image="ghcr.io/unit/api@" + API,
            structural_image="ghcr.io/unit/structural@" + STRUCTURAL,
            document_image="ghcr.io/unit/document@" + DOCUMENT,
            source_url="https://github.com/unit/repo",
            policy=Path("unused"),
            structural_lock=Path("unused"),
            document_lock=Path("unused"),
            models=Path("unused"),
            fixture=Path("unused"),
            expectations=expectations,
            manifest_sha256=MODELS,
        )
    assert not (tmp_path / "document-release-qualification.json").exists()


def test_release_workflow_requires_document_supply_chain_and_final_qualification():
    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text()
    )
    steps = workflow["jobs"]["build-and-qualify"]["steps"]
    build = next(step for step in steps if step.get("id") == "document-build")
    assert build["with"]["file"] == "backend/Dockerfile.document-parser"
    assert "${{ steps.build.outputs.digest }}" in build["with"]["build-args"]
    scan = next(
        step
        for step in steps
        if step["name"] == "Scan the document image for blocking vulnerabilities"
    )
    assert scan["with"]["exit-code"] == "1"
    assert scan["with"]["ignore-unfixed"] is False
    assert scan["with"]["severity"] == "CRITICAL,HIGH"
    required = [
        "Stage approved document models and offline probe inputs",
        "Build the dedicated document image from the exact API digest",
        "Publish document image build provenance",
        "Sign and verify the immutable image",
        "Scan the document image for blocking vulnerabilities",
        "Generate the document SPDX SBOM",
        "Attest and verify the document SBOM",
        "Verify the offline document runtime against approved models and the API parent",
        "Render and verify the three-image document deployment",
        "Stage content-addressed independently reviewed gates",
        "Qualify the complete API and structural image bundle",
        "Qualify all three images and the document model bundle",
    ]
    positions = []
    for name in required:
        step = next(step for step in steps if step["name"] == name)
        assert not step.get("if") and not step.get("continue-on-error")
        positions.append(steps.index(step))
    assert positions == sorted(positions)
    stage = steps[positions[-3]]["run"]
    assert "--hybrid --document" in stage
    final = steps[positions[-1]]["run"]
    for required_input in (
        "--api-image",
        "--structural-image",
        "--document-image",
        "--models",
        "--fixture",
        "--expectations",
        "--model-manifest-sha256",
        "--structural-lock",
        "--document-lock",
    ):
        assert required_input in final
    assert "windops_backend.operations.document_release_gate" in final
    assert ".api_gate_count == 11" in final
