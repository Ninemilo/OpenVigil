"""Strict private input manifest for finite synthetic services measurements."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader

from windops_backend.document_parse_models import verify_model_bundle
from windops_backend.operations.load_contracts import LoadProfile
from windops_backend.operations.load_dataset import canonical_hash
from windops_backend.operations.load_documents import DocumentInput, new_document_id
from windops_backend.operations.load_runner import _strict_json
from windops_backend.operations.load_services_server import checked_workspace_path
from windops_backend.operations.load_software import ROOT


class PlannedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    workload: Literal["document_batch", "backlog"]
    user_id: Literal["a1", "b1"]
    pdf_path: str = Field(min_length=1, max_length=4096, repr=False)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    expected_source_pages: int = Field(ge=1, le=200)
    expected_passages: int = Field(ge=1, le=512)
    classification: Literal["reviewed_synthetic"]


class ServicesInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["openvigil.load-services-inputs.v1"]
    parser_python: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    parser_models: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    parser_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    documents: tuple[PlannedDocument, ...] = Field(default=(), max_length=32)
    backlog_mode: Literal["offline_then_resume", "claim_loss_then_recover"] | None = None
    setup_deadline_seconds: int = Field(default=420, ge=60, le=900)
    evidence_deadline_seconds: int = Field(default=90, ge=15, le=180)
    cleanup_deadline_seconds: int = Field(default=180, ge=90, le=300)
    maximum_container_working_set_bytes: int = Field(default=4 * 1024**3, gt=0, le=64 * 1024**3)
    maximum_total_container_working_set_bytes: int = Field(
        default=12 * 1024**3, gt=0, le=256 * 1024**3
    )

    @model_validator(mode="after")
    def consistent(self) -> Self:
        parser = (self.parser_python, self.parser_models, self.parser_manifest_sha256)
        if bool(self.documents) != all(value is not None for value in parser):
            raise ValueError("documents require a complete parser runtime")
        if not self.documents and any(value is not None for value in parser):
            raise ValueError("unused parser inputs are forbidden")
        if len({d.alias for d in self.documents}) != len(self.documents):
            raise ValueError("document aliases must be unique")
        if (self.backlog_mode is not None) != any(d.workload == "backlog" for d in self.documents):
            raise ValueError("backlog input and fault mode must agree")
        if (
            self.maximum_total_container_working_set_bytes
            < self.maximum_container_working_set_bytes
        ):
            raise ValueError("total container budget cannot be smaller than one container")
        return self


def parse_services_inputs(raw: bytes) -> ServicesInputs:
    if len(raw) > 65536:
        raise ValueError("services input manifest exceeds limit")
    _strict_json(raw)
    return ServicesInputs.model_validate_json(raw)


def validate_plan(profile: LoadProfile, inputs: ServicesInputs) -> None:
    # Revalidate even if a trusted Python caller used model_copy/model_construct.
    profile = LoadProfile.model_validate_json(profile.model_dump_json())
    inputs = parse_services_inputs(inputs.model_dump_json().encode())
    if profile.mode != "services":
        raise ValueError("services execution requires a services profile")
    if profile.history_rows < 90:
        raise ValueError("services dataset requires 90..1000000 synthetic source rows")
    if len(inputs.documents) != profile.documents:
        raise ValueError("profile document count must cover the exact manifest")
    expected_workloads = {"document_batch", "backlog"}.intersection(profile.workloads)
    if {d.workload for d in inputs.documents} != expected_workloads:
        raise ValueError("every document workload needs its own registered inputs")
    if sum(d.size_bytes for d in inputs.documents) > profile.maximum_upload_bytes:
        raise ValueError("document input bytes exceed the registered profile")
    for workload in expected_workloads:
        if {d.user_id for d in inputs.documents if d.workload == workload} != {"a1", "b1"}:
            raise ValueError("each document workload must exercise two authorization domains")
    if inputs.backlog_mode is not None and profile.maximum_duration_seconds < (
        600 if inputs.backlog_mode == "claim_loss_then_recover" else 180
    ):
        raise ValueError("backlog profile cannot cover actual TTL or lease recovery")
    read_routes = (4 if "queries" in profile.workloads else 0) + (
        2 if "history" in profile.workloads else 0
    )
    minimum_http = (
        read_routes * profile.requests_per_route
        + profile.warmup_requests
        + len(inputs.documents) * 5
        + (512 if inputs.backlog_mode is not None else 0)
    )
    if minimum_http > profile.maximum_requests:
        raise ValueError("request cap cannot cover actual read routes and document stages")


def _path(value: str, *, directory: bool = False) -> Path:
    path = Path(value)
    return checked_workspace_path(path if path.is_absolute() else ROOT / path, directory=directory)


def input_identity(inputs: ServicesInputs) -> dict[str, Any]:
    """Comparison identity excludes private paths and random runtime document IDs."""
    public = inputs.model_dump(mode="json", exclude={"parser_python", "parser_models", "documents"})
    public["documents"] = [
        d.model_dump(mode="json", exclude={"pdf_path"}) for d in inputs.documents
    ]
    return {"sha256": canonical_hash(public), "manifest": public}


def prepare_inputs(
    inputs: ServicesInputs, run_id: str
) -> tuple[dict[str, DocumentInput], dict[str, Any]]:
    """Verify pinned local bytes/geometry/model bundle; no OCR execution is implied."""
    documents: dict[str, DocumentInput] = {}
    source_receipts: list[dict[str, Any]] = []
    for planned in inputs.documents:
        source = _path(planned.pdf_path)
        if source.stat().st_size != planned.size_bytes:
            raise ValueError("source size differs from manifest")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
        if digest.hexdigest() != planned.artifact_sha256:
            raise ValueError("source SHA differs from manifest")
        with source.open("rb") as stream:
            pdf = PdfReader(stream, strict=True)
            if pdf.is_encrypted or len(pdf.pages) != planned.expected_source_pages:
                raise ValueError("PDF page identity differs from manifest")
            pages = []
            for page in pdf.pages:
                rectangle = [float(v) for v in page.mediabox]
                if (
                    not all(math.isfinite(v) for v in rectangle)
                    or rectangle[2] <= rectangle[0]
                    or rectangle[3] <= rectangle[1]
                    or rectangle[2] - rectangle[0] > 2000
                    or rectangle[3] - rectangle[1] > 2000
                ):
                    raise ValueError("invalid PDF page geometry")
                pages.append({"media_box": rectangle, "rotation": page.rotation})
        scope = planned.user_id[0]
        documents[planned.alias] = DocumentInput(
            document_id=new_document_id(run_id),
            user_id=planned.user_id,
            path=source,
            artifact_sha256=planned.artifact_sha256,
            size_bytes=planned.size_bytes,
            tenant_id=f"tenant-{scope}",
            wind_farm_id=f"farm-{scope}",
            turbine_id=f"turbine-{scope}",
        )
        source_receipts.append(
            {
                "alias": planned.alias,
                "sha256": digest.hexdigest(),
                "size_bytes": planned.size_bytes,
                "pages": pages,
            }
        )
    parser: dict[str, Any] | None = None
    if inputs.parser_python is not None and inputs.parser_models is not None:
        python = _path(inputs.parser_python)
        models = _path(inputs.parser_models, directory=True)
        if inputs.parser_manifest_sha256 is None:
            raise ValueError("model manifest identity required")
        verify_model_bundle(models, inputs.parser_manifest_sha256)
        parser = {
            "python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
            "model_manifest_sha256": inputs.parser_manifest_sha256,
            "runtime_execution_verified": False,
        }
    return documents, {
        "sources": source_receipts,
        "parser": parser,
        "input_identity": input_identity(inputs),
    }


def parser_paths(inputs: ServicesInputs) -> tuple[Path | None, Path | None]:
    return (
        _path(inputs.parser_python) if inputs.parser_python is not None else None,
        _path(inputs.parser_models, directory=True) if inputs.parser_models is not None else None,
    )
