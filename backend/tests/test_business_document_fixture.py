import hashlib
import json
import shutil

import pytest
from pypdf import PdfReader
from scripts.prepare_business_document_fixture import FIXTURE_ROOT, prepare_document_fixture

from test_document_parse_contract import model_bundle
from windops_backend.operations.document_smoke import DocumentSmokeExpectation


def test_committed_scan_is_an_image_only_two_page_pdf_with_frozen_bytes():
    expected = DocumentSmokeExpectation.model_validate_json(
        (FIXTURE_ROOT / "expectations.json").read_bytes()
    )
    path = FIXTURE_ROOT / "scanned-zh.pdf"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected.fixture_sha256
    pdf = PdfReader(path)
    assert len(pdf.pages) == expected.source_page_count == 2
    assert all(not page.extract_text().strip() for page in pdf.pages)
    assert len(pdf.pages[0].images) == 0 and len(pdf.pages[1].images) == 1
    assert expected.passage_pages == [2]
    assert expected.passage_count == 15 and expected.table_cell_count == 9


@pytest.mark.parametrize("violation", [None, "model_manifest", "model_bytes", "scan", "existing"])
def test_fixture_preparation_revalidates_model_and_scan_bytes(tmp_path, violation):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    models = tmp_path / "models"
    models.mkdir()
    model_bundle(models)
    shutil.copyfile(models / "openvigil-model-manifest.json", fixtures / "model-manifest.json")
    raw = b"synthetic unit-only source bytes"
    (fixtures / "scanned-zh.pdf").write_bytes(raw)
    (fixtures / "expectations.json").write_text(
        json.dumps(
            {
                "schema_version": "openvigil.document-smoke.v1",
                "fixture_sha256": hashlib.sha256(raw).hexdigest(),
                "source_page_count": 2,
                "passage_count": 1,
                "table_cell_count": 0,
                "passage_pages": [2],
                "required_text": ["unit"],
            }
        )
    )
    destination = tmp_path / "staged"
    if violation == "model_manifest":
        (models / "openvigil-model-manifest.json").write_text("changed")
    elif violation == "model_bytes":
        (models / "model-0.bin").write_bytes(b"changed")
    elif violation == "scan":
        (fixtures / "scanned-zh.pdf").write_bytes(b"changed")
    elif violation == "existing":
        destination.mkdir()
        (destination / "keep").write_text("keep")
    if violation:
        with pytest.raises(ValueError):
            prepare_document_fixture(models, destination, fixture_root=fixtures)
        if violation == "existing":
            assert (destination / "keep").read_text() == "keep"
        else:
            assert not destination.exists()
    else:
        prepare_document_fixture(models, destination, fixture_root=fixtures)
        assert (destination / "source.pdf").read_bytes() == raw
