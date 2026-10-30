"""Offline OCR release probe with explicit source and model identities."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import platform
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from windops_backend.document_parse_contract import DocumentParseResult
from windops_backend.document_parse_identity import DOCUMENT_PARSER_VERSIONS, parser_code_identity
from windops_backend.document_parse_models import verify_model_bundle
from windops_backend.document_parse_process import PACKAGE_PATH, parse_document_subprocess
from windops_backend.document_parse_result_identity import document_parse_result_hash
from windops_backend.operations.report_io import sha256_file


class DocumentSmokeExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: str = Field(pattern=r"^openvigil\.document-smoke\.v1$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page_count: int = Field(ge=1, le=200)
    passage_count: int = Field(ge=1, le=512)
    table_cell_count: int = Field(ge=0, le=512)
    passage_pages: list[int] = Field(min_length=1, max_length=200)
    required_text: list[str] = Field(min_length=1, max_length=50)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(f"document runtime smoke failed: {code}")


def validate_document_smoke_result(
    result: DocumentParseResult, expected: DocumentSmokeExpectation, manifest_sha256: str
) -> dict[str, Any]:
    _require(result.source_sha256 == expected.fixture_sha256, "fixture_identity")
    _require(result.model_manifest_sha256 == manifest_sha256, "model_identity")
    _require(result.source_page_count == expected.source_page_count, "page_count")
    _require(len(result.passages) == expected.passage_count, "passage_count")
    _require(
        sum("table_cell" in piece.native_locator for piece in result.passages)
        == expected.table_cell_count,
        "table_cells",
    )
    _require(
        sorted({piece.page_number for piece in result.passages}) == expected.passage_pages,
        "source_page_locations",
    )
    _require(all(text and text in result.body for text in expected.required_text), "required_text")
    _require(
        all(
            piece.native_locator.get("requires_numeric_review") is True for piece in result.passages
        ),
        "numeric_review_required",
    )
    _require(result.execution_code_sha256 == parser_code_identity(), "execution_code")
    _require(result.library_versions == DOCUMENT_PARSER_VERSIONS, "numerical_versions")
    return {
        "source_sha256": result.source_sha256,
        "model_manifest_sha256": manifest_sha256,
        "source_page_count": result.source_page_count,
        "passage_count": len(result.passages),
        "table_cell_count": expected.table_cell_count,
        "execution_code_sha256": result.execution_code_sha256,
        "library_versions": result.library_versions,
        "result_sha256": document_parse_result_hash(result.model_dump(mode="json")),
        "numeric_review_required": True,
    }


def document_runtime_smoke(
    *, models: Path, fixture: Path, expectations: Path, manifest_sha256: str
) -> dict[str, Any]:
    _require(platform.system() == "Linux" and platform.machine() == "x86_64", "platform")
    uid, gid = (int(getattr(os, name)()) for name in ("getuid", "getgid"))
    _require(uid == gid == 10001, "uid_gid")
    _require(importlib.util.find_spec("pytest") is None, "test_dependencies_absent")
    _require(importlib.util.find_spec("docling") is None, "api_dependencies_isolated")
    _require(expectations.stat().st_size <= 64 * 1024, "expectations_size")
    expected = DocumentSmokeExpectation.model_validate_json(expectations.read_bytes())
    _require(sha256_file(fixture) == expected.fixture_sha256, "fixture_before_parse")
    verify_model_bundle(models, manifest_sha256)
    python = "/opt/document-parser/bin/python"
    subprocess.run([python, "-m", "pip", "check"], check=True, timeout=30)  # nosec B603
    # Import only the installed application package into the isolated numerical environment.
    probe = """
import importlib.util,json,pathlib,sys
package_path=pathlib.Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('windops_backend',package_path/'__init__.py',submodule_search_locations=[str(package_path)])
if spec is None or spec.loader is None:
    raise RuntimeError('package bootstrap unavailable')
package=importlib.util.module_from_spec(spec)
sys.modules['windops_backend']=package
spec.loader.exec_module(package)
from windops_backend.structural_dependencies import verify_installed_lock
print(json.dumps(verify_installed_lock(pathlib.Path('/app/requirements.document-parser.txt'))))
"""
    completed = subprocess.run(  # nosec B603
        [python, "-I", "-c", probe, PACKAGE_PATH],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    closure = json.loads(completed.stdout)
    result = asyncio.run(
        parse_document_subprocess(
            fixture.read_bytes(),
            models_path=models,
            manifest_sha256=manifest_sha256,
            python_executable=python,
        )
    )
    verified = validate_document_smoke_result(result, expected, manifest_sha256)
    # Recheck immutable inputs after computation, not just before launching the child.
    verify_model_bundle(models, manifest_sha256)
    _require(sha256_file(fixture) == expected.fixture_sha256, "fixture_after_parse")
    return {
        "platform": "linux/amd64",
        "uid": uid,
        "gid": gid,
        "dependency_closure": closure,
        "expectations_sha256": sha256_file(expectations),
        "parse": verified,
        "pip_check": "passed",
        "boundary": (
            "offline synthetic OCR software probe; not field accuracy or release qualification"
        ),
        "field_qualification": "unverified",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("models", "fixture", "expectations"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--model-manifest-sha256", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            document_runtime_smoke(
                models=args.models,
                fixture=args.fixture,
                expectations=args.expectations,
                manifest_sha256=args.model_manifest_sha256,
            )
        )
    )


if __name__ == "__main__":
    main()
