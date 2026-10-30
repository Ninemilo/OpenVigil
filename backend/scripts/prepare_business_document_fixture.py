"""Validate pinned CI model bytes and copy the reviewed synthetic scan to owned scratch."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from windops_backend.document_parse_models import verify_model_bundle
from windops_backend.operations.document_smoke import DocumentSmokeExpectation
from windops_backend.operations.report_io import sha256_file

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/document"


def prepare_document_fixture(
    models: Path, destination: Path, *, fixture_root: Path = FIXTURE_ROOT
) -> None:
    if destination.exists() or destination.is_symlink():
        raise ValueError("document CI fixture destination must be new")
    manifest = fixture_root / "model-manifest.json"
    expected = DocumentSmokeExpectation.model_validate_json(
        (fixture_root / "expectations.json").read_bytes()
    )
    verify_model_bundle(models, sha256_file(manifest))
    source = fixture_root / "scanned-zh.pdf"
    if sha256_file(source) != expected.fixture_sha256:
        raise ValueError("document CI scan differs from the reviewed source")
    destination.mkdir(parents=True)
    shutil.copyfile(source, destination / "source.pdf")
    shutil.copyfile(fixture_root / "expectations.json", destination / "expectations.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    prepare_document_fixture(args.models, args.destination)


if __name__ == "__main__":
    main()
