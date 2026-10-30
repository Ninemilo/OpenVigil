"""Stage an approved model bundle and synthetic scan without trusting archive paths."""

from __future__ import annotations

import argparse
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from windops_backend.document_parse_contract import MAX_INPUT_BYTES
from windops_backend.document_parse_models import MAX_MODEL_BUNDLE_BYTES, verify_model_bundle
from windops_backend.operations.document_smoke import DocumentSmokeExpectation
from windops_backend.operations.report_io import sha256_file

MAX_BYTES = MAX_MODEL_BUNDLE_BYTES + MAX_INPUT_BYTES + 1024 * 1024


def stage_document_inputs(
    archive: Path, destination: Path, *, archive_sha256: str, manifest_sha256: str
) -> None:
    if archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= MAX_BYTES:
        raise ValueError("document input archive is missing, linked or oversized")
    if sha256_file(archive) != archive_sha256:
        raise ValueError("document input archive does not match protected configuration")
    if (
        destination.exists()
        or destination.is_symlink()
        or any(parent.is_symlink() for parent in destination.parents)
    ):
        raise ValueError("document input destination must be new")
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if not 1 <= len(members) <= 512:
            raise ValueError("document input archive has an invalid member count")
        paths: set[str] = set()
        size = 0
        # Validate every member before writing any of the input files.
        for member in members:
            path = PurePosixPath(member.filename)
            if (
                member.orig_filename != member.filename
                or "\x00" in member.orig_filename
                or "\\" in member.orig_filename
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in member.filename
                or ":" in member.filename
                or not path.parts
                or path.as_posix() != member.filename.rstrip("/")
                or member.filename in paths
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError("document input archive contains an unsafe or duplicate path")
            paths.add(member.filename)
            if member.is_dir():
                continue
            allowed = (len(path.parts) >= 2 and path.parts[0] == "models") or path.as_posix() in {
                "fixture/source.pdf",
                "fixture/expectations.json",
            }
            if not allowed or not 0 < member.file_size <= MAX_MODEL_BUNDLE_BYTES:
                raise ValueError("document input archive contains an unexpected or oversized file")
            if path.as_posix() == "fixture/source.pdf" and member.file_size > MAX_INPUT_BYTES:
                raise ValueError("document probe exceeds the parser input limit")
            if path.as_posix() == "fixture/expectations.json" and member.file_size > 64 * 1024:
                raise ValueError("document expectations exceed the size limit")
            size += member.file_size
            if size > MAX_BYTES:
                raise ValueError("document input archive expands beyond the limit")
        destination.mkdir(parents=True)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.filename).parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    verify_model_bundle(destination / "models", manifest_sha256)
    expected = DocumentSmokeExpectation.model_validate_json(
        (destination / "fixture/expectations.json").read_bytes()
    )
    if sha256_file(destination / "fixture/source.pdf") != expected.fixture_sha256:
        raise ValueError("document probe fixture does not match its approved expectations")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--model-manifest-sha256", required=True)
    args = parser.parse_args()
    stage_document_inputs(
        args.archive,
        args.destination,
        archive_sha256=args.archive_sha256,
        manifest_sha256=args.model_manifest_sha256,
    )


if __name__ == "__main__":
    main()
