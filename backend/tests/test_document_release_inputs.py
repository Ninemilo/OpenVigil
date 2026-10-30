"""Archive and identity tests with explicit unit bytes, never OCR acceptance."""

import hashlib
import json
import stat
import zipfile

import pytest
from scripts.stage_document_release_inputs import stage_document_inputs
from scripts.stage_independent_release_evidence import INDEPENDENT_GATES, stage_independent_evidence

from test_document_parse_contract import model_bundle
from windops_backend.operations.report_io import sha256_file


def _archive(tmp_path, *, extra=None, omit=None, altered=None):
    source = tmp_path / "source"
    models = source / "models"
    models.mkdir(parents=True)
    _, digest = model_bundle(models)
    fixture = source / "fixture"
    fixture.mkdir()
    content = b"synthetic unit contract bytes, not a real PDF"
    (fixture / "source.pdf").write_bytes(content)
    (fixture / "expectations.json").write_text(
        json.dumps(
            {
                "schema_version": "openvigil.document-smoke.v1",
                "fixture_sha256": hashlib.sha256(content).hexdigest(),
                "source_page_count": 2,
                "passage_count": 1,
                "table_cell_count": 0,
                "passage_pages": [2],
                "required_text": ["unit"],
            }
        )
    )
    archive = tmp_path / "inputs.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in source.rglob("*"):
            if path.is_file():
                name = path.relative_to(source).as_posix()
                if name != omit:
                    bundle.writestr(name, b"altered" if name == altered else path.read_bytes())
        if extra:
            # ZipInfo normalizes backslashes on Windows; retain hostile raw bytes in this fixture.
            info = zipfile.ZipInfo("unit")
            info.filename = extra
            info.orig_filename = extra
            bundle.writestr(info, "unit")
    return archive, digest


def test_approved_inputs_stage_exact_bytes_and_verify_models_and_fixture(tmp_path):
    archive, digest = _archive(tmp_path)
    target = tmp_path / "staged"
    stage_document_inputs(
        archive, target, archive_sha256=sha256_file(archive), manifest_sha256=digest
    )
    source = tmp_path / "source"
    for file in source.rglob("*"):
        if file.is_file():
            assert (target / file.relative_to(source)).read_bytes() == file.read_bytes()
    with pytest.raises(ValueError, match="must be new"):
        stage_document_inputs(
            archive, target, archive_sha256=sha256_file(archive), manifest_sha256=digest
        )


@pytest.mark.parametrize(
    "extra",
    [
        "../escape",
        "/absolute",
        "models/../escape",
        "models/a\\b",
        "models/C:stream",
        "unapproved.txt",
        "models//double",
        "models/./dot",
    ],
)
def test_input_archive_rejects_unsafe_paths_before_extracting(tmp_path, extra):
    archive, digest = _archive(tmp_path, extra=extra)
    target = tmp_path / "staged"
    with pytest.raises(ValueError):
        stage_document_inputs(
            archive, target, archive_sha256=sha256_file(archive), manifest_sha256=digest
        )
    assert not target.exists()


@pytest.mark.parametrize(
    "violation", ["archive", "model", "model_bytes", "fixture", "missing", "symlink", "duplicate"]
)
def test_input_archive_rejects_changed_or_missing_content(tmp_path, violation):
    archive, digest = _archive(
        tmp_path,
        omit="fixture/source.pdf" if violation == "missing" else None,
        altered={"model_bytes": "models/model-0.bin", "fixture": "fixture/source.pdf"}.get(
            violation
        ),
    )
    if violation in {"symlink", "duplicate"}:
        with zipfile.ZipFile(archive, "a") as bundle:
            if violation == "symlink":
                info = zipfile.ZipInfo("models/linked")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(info, "../elsewhere")
            else:
                with pytest.warns(UserWarning, match="Duplicate name"):
                    bundle.writestr("models/model-0.bin", "duplicate")
    with pytest.raises((ValueError, OSError)):
        stage_document_inputs(
            archive,
            tmp_path / "staged",
            archive_sha256="0" * 64 if violation == "archive" else sha256_file(archive),
            manifest_sha256="0" * 64 if violation == "model" else digest,
        )


@pytest.mark.parametrize("missing", [None, "structural_runtime", "document_runtime", "migration"])
def test_document_independent_bundle_requires_all_previous_and_document_reports(tmp_path, missing):
    archive = tmp_path / "independent.zip"
    required = INDEPENDENT_GATES | {"structural_runtime", "document_runtime"}
    with zipfile.ZipFile(archive, "w") as bundle:
        for gate in required - {missing}:
            bundle.writestr(f"reports/{gate}.json", '{"unit_staging_only": true}')
    if missing:
        with pytest.raises(ValueError, match="missing reports"):
            stage_independent_evidence(archive, tmp_path / "staged", document=True)
    else:
        stage_independent_evidence(archive, tmp_path / "staged", document=True)
        assert {path.stem for path in (tmp_path / "staged/reports").glob("*.json")} == required
