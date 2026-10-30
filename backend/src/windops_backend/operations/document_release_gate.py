"""Qualify OCR only with the API/structural gates and independent document evidence."""

from __future__ import annotations

import argparse
import copy
import json
import xml.etree.ElementTree as ET  # nosec B405
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from windops_backend.document_parse_contract import MAX_OUTPUT_BYTES, DocumentParseResult
from windops_backend.operations.deployment_policy import verify_deployment_policy
from windops_backend.operations.document_artifact import verify_document_artifact
from windops_backend.operations.document_identity import document_bundle_id
from windops_backend.operations.document_smoke import (
    DocumentSmokeExpectation,
    validate_document_smoke_result,
)
from windops_backend.operations.hybrid_identity import hybrid_bundle_id
from windops_backend.operations.hybrid_release_gate import (
    verify_hybrid_release,
    verify_runtime_supply_chain,
)
from windops_backend.operations.release_gate import (
    GATE_REPORT_SCHEMA,
    MANIFEST_NAME,
    MAX_EVIDENCE_SPAN_SECONDS,
    MAX_REPORT_DURATION_SECONDS,
    _aware_datetime,
    _safe_report_path,
    _verify_image_scan_raw,
)
from windops_backend.operations.report_io import sha256_file, write_atomic_json

DOCUMENT_CHECKS = frozenset(
    {
        "three_image_model_runtime_identity",
        "docling_ocr_table_pages",
        "scope_and_source_revalidation",
        "retry_timeout_queue_recovery",
        "original_object_sha_roundtrip",
        "idempotency_and_concurrency",
        "numeric_review_required",
        "audit_sql_outbox_index_consistency",
    }
)
PARSED_RESULT = "artifacts/document/parsed-result.json"
DOCUMENT_ACCEPTANCE_SCHEMA = copy.deepcopy(GATE_REPORT_SCHEMA)
DOCUMENT_ACCEPTANCE_SCHEMA["properties"]["gate"] = {"const": "document_runtime"}
_identity_schema = DOCUMENT_ACCEPTANCE_SCHEMA["properties"]["release"]
for _field in (
    "api_image_digest",
    "structural_image_digest",
    "hybrid_bundle_id",
    "bundle_id",
    "model_manifest_sha256",
):
    _identity_schema["required"].append(_field)
    _identity_schema["properties"][_field] = {
        "type": "string",
        "pattern": ("^" if _field == "model_manifest_sha256" else "^sha256:") + "[0-9a-f]{64}$",
    }


def _verified_junit_cases(path: Path) -> set[tuple[str, str]]:
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError("document JUnit is unsafe or oversized")
    junit = ET.fromstring(raw)  # nosec B314
    if junit.tag not in {"testsuite", "testsuites"}:
        raise ValueError("document JUnit requires a test suite root")
    if any(junit.findall(".//" + name) for name in ("failure", "error", "skipped")):
        raise ValueError("document acceptance contains failed or skipped cases")
    for suite in [junit, *junit.findall(".//testsuite")]:
        if any(int(suite.get(name, "0")) != 0 for name in ("failures", "errors", "skipped")):
            raise ValueError("document acceptance reports failed or skipped cases")
    cases = set()
    for case in junit.findall(".//testcase"):
        if not case.get("name"):
            raise ValueError("document JUnit requires named cases")
        cases.add((case.get("classname", ""), case.attrib["name"]))
    return cases


def verify_document_acceptance(
    root: Path, identity: dict[str, str], expected: DocumentSmokeExpectation
) -> dict[str, Any]:
    path = _safe_report_path(root, "reports/document_runtime.json")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("document acceptance report exceeds its bound")
    report = json.loads(path.read_bytes())
    errors = sorted(Draft202012Validator(DOCUMENT_ACCEPTANCE_SCHEMA).iter_errors(report), key=str)
    if errors:
        raise ValueError(f"document acceptance schema violation: {errors[0].message}")
    if report["release"] != identity:
        raise ValueError("document acceptance must bind all three images and the model manifest")
    started = _aware_datetime(report["started_at"], "document.started_at")
    completed = _aware_datetime(report["completed_at"], "document.completed_at")
    if not 0 <= (completed - started).total_seconds() <= MAX_REPORT_DURATION_SECONDS:
        raise ValueError("document acceptance duration is invalid")
    if not 0 <= (datetime.now(UTC) - completed).total_seconds() <= MAX_EVIDENCE_SPAN_SECONDS:
        raise ValueError("document acceptance is stale or from the future")
    artifacts: dict[str, Path] = {}
    size = 0
    for artifact in report["artifacts"]:
        relative = artifact["path"]
        candidate = _safe_report_path(root, relative)
        if relative in artifacts or candidate == path or not candidate.is_file():
            raise ValueError("document artifact is duplicate, missing or self-referential")
        if not 0 < candidate.stat().st_size <= 128 * 1024 * 1024:
            raise ValueError("document artifact is empty or oversized")
        size += candidate.stat().st_size
        if size > 512 * 1024 * 1024 or sha256_file(candidate) != artifact["sha256"]:
            raise ValueError("document artifacts exceed the bound or have changed")
        artifacts[relative] = candidate
    checks = report["checks"]
    observed = [check["id"] for check in checks]
    if len(observed) != len(set(observed)) or set(observed) != DOCUMENT_CHECKS:
        raise ValueError("document acceptance requires every check exactly once")
    for check in checks:
        if not set(check["artifact_paths"]).issubset(artifacts):
            raise ValueError("document check cites an undeclared artifact")
        cases: set[tuple[str, str]] = set()
        for relative in check["artifact_paths"]:
            if artifacts[relative].suffix == ".xml":
                cases |= _verified_junit_cases(artifacts[relative])
        if not cases:
            raise ValueError("every document check requires real non-skipped JUnit evidence")
    if PARSED_RESULT not in artifacts or artifacts[PARSED_RESULT].stat().st_size > MAX_OUTPUT_BYTES:
        raise ValueError("document acceptance requires the bounded persisted parse result")
    parsed = DocumentParseResult.model_validate_json(artifacts[PARSED_RESULT].read_bytes())
    validate_document_smoke_result(parsed, expected, identity["model_manifest_sha256"])
    return cast(dict[str, Any], report)


def verify_document_release(
    *,
    evidence_dir: Path,
    release_id: str,
    commit_sha: str,
    api_image: str,
    structural_image: str,
    document_image: str,
    source_url: str,
    policy: Path,
    structural_lock: Path,
    document_lock: Path,
    models: Path,
    fixture: Path,
    expectations: Path,
    manifest_sha256: str,
) -> dict[str, Any]:
    root = evidence_dir.resolve()
    api_digest, structural_digest, document_digest = (
        image.rsplit("@", 1)[-1] for image in (api_image, structural_image, document_image)
    )
    bundle = document_bundle_id(
        release_id, commit_sha, api_digest, structural_digest, document_digest, manifest_sha256
    )
    identity = {
        "release_id": release_id,
        "commit_sha": commit_sha,
        "api_image_digest": api_digest,
        "structural_image_digest": structural_digest,
        "image_digest": document_digest,
        "model_manifest_sha256": manifest_sha256,
        "bundle_id": bundle,
        "evidence_set_id": bundle,
        "hybrid_bundle_id": hybrid_bundle_id(release_id, commit_sha, api_digest, structural_digest),
    }
    if expectations.stat().st_size > 64 * 1024:
        raise ValueError("document expectations exceed their bound")
    expected = DocumentSmokeExpectation.model_validate_json(expectations.read_bytes())
    acceptance = verify_document_acceptance(root, identity, expected)
    # Re-run the prior gates; never trust a saved status or qualification summary.
    hybrid = verify_hybrid_release(
        evidence_dir=root,
        release_id=release_id,
        commit_sha=commit_sha,
        api_image=api_image,
        structural_image=structural_image,
        source_url=source_url,
        policy=policy,
        lock=structural_lock,
    )
    manifest = json.loads((root / MANIFEST_NAME).read_bytes())
    completions = [
        _aware_datetime(item["completed_at"], "api.completed_at") for item in manifest["evidence"]
    ] + [
        _aware_datetime(hybrid["structural_acceptance"]["completed_at"], "structural.completed_at"),
        _aware_datetime(acceptance["completed_at"], "document.completed_at"),
    ]
    if (max(completions) - min(completions)).total_seconds() > MAX_EVIDENCE_SPAN_SECONDS:
        raise ValueError("three-image reports span different release windows")
    deployment = verify_deployment_policy(
        _safe_report_path(root, "document/rendered-manifests.yaml"),
        expected_release_id=release_id,
        expected_commit_sha=commit_sha,
        expected_image_digest=api_digest,
        expected_structural_image_digest=structural_digest,
        expected_document_image_digest=document_digest,
        expected_model_manifest_sha256=manifest_sha256,
        approved_backup_storage_class=json.loads(policy.read_bytes())[
            "approved_backup_storage_class"
        ],
    )
    _verify_image_scan_raw(
        root,
        identity,
        [
            {"id": name, "artifact_paths": ["document/image-scan.json"]}
            for name in ("immutable_digest_scanned", "critical_findings_zero", "high_findings_zero")
        ],
    )
    verify_runtime_supply_chain(root, document_image, source_url, kind="document")
    artifact = verify_document_artifact(
        image=document_image,
        api_image=api_image,
        release_id=release_id,
        commit_sha=commit_sha,
        source_url=source_url,
        policy_path=policy,
        lock_path=document_lock,
        models=models,
        fixture=fixture,
        expectations=expectations,
        manifest_sha256=manifest_sha256,
    )
    # Raw model manifest must also be present in the distributable qualification evidence.
    model_manifest = _safe_report_path(root, "document/model-manifest.json")
    if sha256_file(model_manifest) != manifest_sha256:
        raise ValueError("qualified model manifest differs from the runtime inputs")
    saved_expectations = _safe_report_path(root, "document/probe-expectations.json")
    if sha256_file(saved_expectations) != sha256_file(expectations):
        raise ValueError("qualified probe expectations differ from the approved inputs")
    paths = {
        "reports/document_runtime.json",
        "document/rendered-manifests.yaml",
        "document/image-scan.json",
        "document/sbom.spdx.json",
        "document/model-manifest.json",
        "document/probe-expectations.json",
    }
    paths.update(item["path"] for item in acceptance["artifacts"])
    return {
        "format_version": 1,
        "status": "qualified",
        **identity,
        "api_image": api_image,
        "structural_image": structural_image,
        "document_image": document_image,
        "verified_at": datetime.now(UTC).isoformat(),
        "api_gate_count": hybrid["api_gate_count"],
        "hybrid_qualification": hybrid,
        "document_acceptance": acceptance,
        "document_artifact": artifact,
        "deployment": deployment,
        "artifacts_sha256": {
            relative: sha256_file(_safe_report_path(root, relative)) for relative in sorted(paths)
        },
        "field_qualification": "unverified",
        "verifier": "windops-document-release-gate.v1",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "release-id",
        "commit-sha",
        "api-image",
        "structural-image",
        "document-image",
        "source-url",
        "model-manifest-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    for name in (
        "evidence-dir",
        "policy",
        "structural-lock",
        "document-lock",
        "models",
        "fixture",
        "expectations",
        "qualification-output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.qualification_output.exists() or args.qualification_output.is_symlink():
        raise ValueError("document qualification output already exists or is unsafe")
    report = verify_document_release(
        evidence_dir=args.evidence_dir,
        release_id=args.release_id,
        commit_sha=args.commit_sha,
        api_image=args.api_image,
        structural_image=args.structural_image,
        document_image=args.document_image,
        source_url=args.source_url,
        policy=args.policy,
        structural_lock=args.structural_lock,
        document_lock=args.document_lock,
        models=args.models,
        fixture=args.fixture,
        expectations=args.expectations,
        manifest_sha256=args.model_manifest_sha256,
    )
    write_atomic_json(
        args.qualification_output,
        report,
        symlink_error=ValueError("unsafe document qualification path"),
    )
    print(json.dumps({key: report[key] for key in ("status", "bundle_id", "api_gate_count")}))


if __name__ == "__main__":
    main()
