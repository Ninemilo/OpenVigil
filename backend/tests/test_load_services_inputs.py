"""Real PDF and manifest-byte checks, with explicitly non-executable model fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

import windops_backend.operations.load_services_inputs as inputs_module
import windops_backend.operations.load_services_server as server
from windops_backend.operations.load_contracts import parse_load_profile
from windops_backend.operations.load_services_inputs import (
    input_identity,
    parse_services_inputs,
    prepare_inputs,
    validate_plan,
)


def profile():
    raw = json.loads(
        (Path(__file__).parent / "fixtures/performance/software-small.json").read_bytes()
    )
    raw.update(
        mode="services",
        workloads=["document_batch"],
        documents=2,
        maximum_upload_bytes=1024 * 1024,
        maximum_requests=1000,
        maximum_duration_seconds=180.0,
    )
    raw["budgets"] = [{**raw["budgets"][0], "workload": "document_batch"}]
    return parse_load_profile(json.dumps(raw).encode())


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(inputs_module, "ROOT", tmp_path)
    pdf = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(pdf)
    models = tmp_path / "models"
    models.mkdir()
    files = []
    sources = []
    for i in range(3):
        payload = f"non-executable-model-hash-fixture-{i}".encode()
        name = f"fixture-{i}.bin"
        (models / name).write_bytes(payload)
        files.append(
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
        sources.append(
            {
                "name": f"fixture-{i}",
                "source_url": "https://example.invalid/fixture",
                "revision": "test",
                "license": "test",
                "license_url": "https://example.invalid/license",
            }
        )
    model_bytes = json.dumps(
        {
            "contract": "openvigil.document-models.v1",
            "docling_version": "2.133.0",
            "files": files,
            "sources": sources,
        }
    ).encode()
    (models / "openvigil-model-manifest.json").write_bytes(model_bytes)
    (tmp_path / "parser-fixture").write_bytes(b"not-executed-runtime-fixture")
    document = {
        "workload": "document_batch",
        "pdf_path": "source.pdf",
        "artifact_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "size_bytes": pdf.stat().st_size,
        "expected_source_pages": 1,
        "expected_passages": 1,
        "classification": "reviewed_synthetic",
    }
    return {
        "schema_version": "openvigil.load-services-inputs.v1",
        "parser_python": "parser-fixture",
        "parser_models": "models",
        "parser_manifest_sha256": hashlib.sha256(model_bytes).hexdigest(),
        "documents": [
            {**document, "alias": "doc-a", "user_id": "a1"},
            {**document, "alias": "doc-b", "user_id": "b1"},
        ],
    }


def test_local_pdf_geometry_and_all_pinned_model_bytes_verified(manifest) -> None:
    inputs = parse_services_inputs(json.dumps(manifest).encode())
    validate_plan(profile(), inputs)
    documents, report = prepare_inputs(inputs, "a" * 32)
    assert len(documents) == 2 and len({d.document_id for d in documents.values()}) == 2
    assert documents["doc-a"].tenant_id == "tenant-a"
    assert documents["doc-b"].tenant_id == "tenant-b"
    assert report["sources"][0]["pages"] == [{"media_box": [0.0, 0.0, 612.0, 792.0], "rotation": 0}]
    assert report["parser"]["runtime_execution_verified"] is False
    assert "pdf_path" not in json.dumps(input_identity(inputs))
    assert "parser-fixture" not in json.dumps(report)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("artifact_sha256", "f" * 64, "SHA"),
        ("size_bytes", 1, "size"),
        ("expected_source_pages", 2, "page"),
        ("pdf_path", "../outside.pdf", "workspace"),
    ],
)
def test_bad_source_identity_or_escape_rejected(manifest, field, value, match) -> None:
    manifest["documents"][0][field] = value
    with pytest.raises(ValueError, match=match):
        prepare_inputs(parse_services_inputs(json.dumps(manifest).encode()), "a" * 32)


def test_changed_model_file_rejected(manifest, tmp_path) -> None:
    (tmp_path / "models/fixture-0.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="model file size"):
        prepare_inputs(parse_services_inputs(json.dumps(manifest).encode()), "a" * 32)


@pytest.mark.parametrize(
    "case",
    ["one_scope", "too_many", "upload_cap", "extra_workload", "short_recovery", "read_request_cap"],
)
def test_plan_limits_cover_actual_routes_two_domains_and_fault_wait(manifest, case) -> None:
    raw_profile = json.loads(profile().model_dump_json())
    if case == "one_scope":
        manifest["documents"][1]["user_id"] = "a1"
    elif case == "too_many":
        raw_profile["documents"] = 1
    elif case == "upload_cap":
        raw_profile["maximum_upload_bytes"] = 1
    elif case == "extra_workload":
        manifest["documents"][1]["workload"] = "backlog"
        manifest["backlog_mode"] = "offline_then_resume"
    elif case == "short_recovery":
        for document in manifest["documents"]:
            document["workload"] = "backlog"
        manifest["backlog_mode"] = "claim_loss_then_recover"
        raw_profile["workloads"] = ["backlog"]
        raw_profile["budgets"][0]["workload"] = "backlog"
    else:
        raw_profile.update(
            workloads=["queries"], maximum_requests=104, documents=0, maximum_upload_bytes=0
        )
        raw_profile["budgets"][0]["workload"] = "queries"
        manifest = {"schema_version": "openvigil.load-services-inputs.v1"}
    with pytest.raises(ValueError):
        validate_plan(
            parse_load_profile(json.dumps(raw_profile).encode()),
            parse_services_inputs(json.dumps(manifest).encode()),
        )


def test_duplicate_keys_and_unknown_fields_rejected() -> None:
    with pytest.raises(ValueError):
        parse_services_inputs(
            b'{"schema_version":"x","schema_version":"openvigil.load-services-inputs.v1"}'
        )
    with pytest.raises(ValueError):
        parse_services_inputs(
            b'{"schema_version":"openvigil.load-services-inputs.v1","url":"https://outside.invalid"}'
        )
