"""Separate, hash-bound source and label contracts for workflow evaluation.

Labels are declarations to be scored after execution, never operational facts.
Review metadata records provenance; it does not establish expert qualification.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from windops_backend.knowledge_graph.domain import GraphAccessPolicy
from windops_backend.schemas import MissionCreateRequest, ScadaSampleIn

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")]
ShortId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,35}$")]
ReviewType = Literal["engineering", "safety", "economic", "resource", "compliance"]


def read_bundle_bytes(path: Path) -> bytes:
    """Bounded regular-file reads with duplicate/non-finite JSON rejection."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("evaluation bundle must be a regular file")
    for part in (path.absolute(), *path.absolute().parents):
        attributes = part.lstat()
        if part.is_symlink() or getattr(attributes, "st_file_attributes", 0) & (
            stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("evaluation bundle cannot use filesystem links")
    with path.open("rb") as handle:
        raw = handle.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("evaluation bundle exceeds 16 MiB")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key in evaluation bundle")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON value in evaluation bundle")

    json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    return raw


def canonical_sha256(value: BaseModel) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class WorkflowCallerScope(SourceRecord):
    tenant_ids: list[Identifier] = Field(default_factory=list, max_length=32)
    wind_farm_ids: list[ShortId] = Field(default_factory=list, max_length=32)
    turbine_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    entity_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    data_scopes: list[Identifier] = Field(default_factory=list, max_length=32)
    allow_global: bool = False

    def to_policy(self) -> GraphAccessPolicy:
        return GraphAccessPolicy.from_values(**self.model_dump())

    @model_validator(mode="after")
    def require_explicit_asset_scope(self) -> Self:
        if not (self.tenant_ids or self.wind_farm_ids or self.turbine_ids or self.entity_ids):
            raise ValueError("caller scope must include an explicit asset grant")
        return self


class TenantInput(SourceRecord):
    id: Identifier
    name: str = Field(min_length=1, max_length=160)


class FarmInput(SourceRecord):
    id: ShortId
    tenant_id: Identifier
    name: str = Field(min_length=1, max_length=160)
    capacity_mw: float = Field(gt=0)


class TurbineInput(SourceRecord):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
    wind_farm_id: ShortId
    model: str = Field(min_length=1, max_length=80)
    status: Literal["running", "warning", "maintenance", "offline"]
    health_score: float = Field(ge=0, le=100)


class TopologyInput(SourceRecord):
    tenants: list[TenantInput] = Field(min_length=1, max_length=32)
    wind_farms: list[FarmInput] = Field(min_length=1, max_length=32)
    turbines: list[TurbineInput] = Field(min_length=1, max_length=64)


class IngestSourceInput(SourceRecord):
    id: Identifier
    display_name: str = Field(min_length=1, max_length=160)
    source_kind: str = Field(min_length=1, max_length=32)


class ReceiptInput(SourceRecord):
    source_id: Identifier
    received_at: AwareDatetime
    payload: ScadaSampleIn


class SampleInput(SourceRecord):
    sample_id: ShortId
    source_event_id: str = Field(min_length=3, max_length=128)


class AlarmInput(SourceRecord):
    id: ShortId
    source_event_id: str = Field(min_length=3, max_length=128)
    code: str = Field(min_length=1, max_length=64)
    subsystem: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=3, max_length=240)
    severity: Literal["critical", "major", "minor", "warning", "info"]


class KnowledgeInput(SourceRecord):
    id: Identifier
    tenant_id: Identifier | None = None
    wind_farm_id: ShortId | None = None
    turbine_id: str | None = Field(default=None, max_length=32)
    data_scope: Literal["knowledge"] = "knowledge"
    title: str = Field(min_length=1, max_length=240)
    document_type: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=200_000)
    body_sha256: Sha256
    document_version: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def verify_body(self) -> Self:
        if hashlib.sha256(self.body.encode("utf-8")).hexdigest() != self.body_sha256:
            raise ValueError("knowledge body SHA-256 mismatch")
        return self


class ResourceInput(SourceRecord):
    id: Identifier
    resource_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    quantity: int = Field(ge=0, le=100_000)
    status: Literal["available", "reserved"]


class WeatherInput(SourceRecord):
    id: ShortId
    wind_farm_id: ShortId
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    wind_speed_ms: float = Field(ge=0)
    wave_height_m: float = Field(ge=0)
    suitable: bool

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("weather window must end after it starts")
        return self


class WorkflowFacts(SourceRecord):
    topology: TopologyInput
    ingest_sources: list[IngestSourceInput] = Field(min_length=1, max_length=32)
    receipts: list[ReceiptInput] = Field(min_length=1, max_length=10_000)
    samples: list[SampleInput] = Field(default_factory=list, max_length=10_000)
    alarm: AlarmInput
    mission_request: MissionCreateRequest
    knowledge_documents: list[KnowledgeInput] = Field(default_factory=list, max_length=128)
    resources: list[ResourceInput] = Field(default_factory=list, max_length=128)
    weather: list[WeatherInput] = Field(default_factory=list, max_length=128)
    caller_scope: WorkflowCallerScope

    @model_validator(mode="after")
    def validate_relations(self) -> Self:
        def unique(values: list[str]) -> set[str]:
            if len({value.casefold() for value in values}) != len(values):
                raise ValueError("duplicate source identity")
            return set(values)

        tenants = unique([row.id for row in self.topology.tenants])
        farms = unique([row.id for row in self.topology.wind_farms])
        turbines = unique([row.id for row in self.topology.turbines])
        sources = unique([row.id for row in self.ingest_sources])
        receipts = unique([row.payload.source_event_id for row in self.receipts])
        unique([row.sample_id for row in self.samples])
        unique([row.source_event_id for row in self.samples])
        unique([row.id for row in self.knowledge_documents])
        unique([row.id for row in self.resources])
        unique([row.id for row in self.weather])
        if any(row.tenant_id not in tenants for row in self.topology.wind_farms):
            raise ValueError("farm tenant is absent")
        if any(row.wind_farm_id not in farms for row in self.topology.turbines):
            raise ValueError("turbine farm is absent")
        if any(
            row.source_id not in sources or row.payload.turbine_id not in turbines
            for row in self.receipts
        ):
            raise ValueError("receipt source or turbine is absent")
        if any(row.source_event_id not in receipts for row in self.samples):
            raise ValueError("sample receipt is absent")
        if self.alarm.source_event_id not in receipts:
            raise ValueError("alarm receipt is absent")
        if self.mission_request.alarm_id != self.alarm.id:
            raise ValueError("mission alarm mismatch")
        profile = self.mission_request.analysis_profile
        if profile.work_order_plan is None:
            raise ValueError("evaluation requires an explicit work order plan")
        if profile.failure_mode_hint:
            raise ValueError("evaluation cannot supply a failure-mode answer hint")
        if profile.work_order_plan.closure_kind != "health_score":
            raise ValueError("structural missions require a separate governed source contract")
        if any(row.wind_farm_id not in farms for row in self.weather):
            raise ValueError("weather farm is absent")
        farm_tenants = {row.id: row.tenant_id for row in self.topology.wind_farms}
        turbine_farms = {row.id: row.wind_farm_id for row in self.topology.turbines}
        for document in self.knowledge_documents:
            if document.tenant_id is not None and document.tenant_id not in tenants:
                raise ValueError("document tenant is absent")
            if document.wind_farm_id is not None:
                if farm_tenants.get(document.wind_farm_id) != document.tenant_id:
                    raise ValueError("document farm tenant mismatch")
            if document.turbine_id is not None:
                if turbine_farms.get(document.turbine_id) != document.wind_farm_id:
                    raise ValueError("document turbine farm mismatch")
        return self


class SourceArtifact(SourceRecord):
    path: str = Field(min_length=1, max_length=512)
    sha256: Sha256
    size_bytes: int = Field(ge=1, le=64 * 1024 * 1024)

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if any(part in {"", ".", ".."} for part in value.split("/")) or any(
            char in value for char in "\\:\x00"
        ):
            raise ValueError("source artifact path must be a plain relative POSIX path")
        return value


class SourceProvenance(SourceRecord):
    kind: Literal["synthetic_fixture", "deidentified_field_snapshot"]
    source_summary: str = Field(min_length=10, max_length=2000)
    source_snapshot_sha256: Sha256
    source_artifacts: list[SourceArtifact] = Field(default_factory=list, max_length=128)
    deidentification_statement: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_field_provenance(self) -> Self:
        if self.kind == "deidentified_field_snapshot" and (
            not self.source_artifacts or not self.deidentification_statement.strip()
        ):
            raise ValueError("field snapshots require source artifacts and deidentification")
        if sum(row.size_bytes for row in self.source_artifacts) > 256 * 1024 * 1024:
            raise ValueError("source artifacts exceed the 256 MiB case limit")
        if len({row.path.casefold() for row in self.source_artifacts}) != len(
            self.source_artifacts
        ):
            raise ValueError("duplicate source artifact path")
        return self


class WorkflowCaseInput(SourceRecord):
    case_id: Identifier
    as_of: AwareDatetime
    source: SourceProvenance
    facts: WorkflowFacts

    @model_validator(mode="after")
    def verify_snapshot(self) -> Self:
        if canonical_sha256(self.facts) != self.source.source_snapshot_sha256:
            raise ValueError("source snapshot SHA-256 mismatch")
        for receipt in self.facts.receipts:
            if (
                receipt.payload.observed_at > receipt.received_at
                or receipt.received_at > self.as_of
            ):
                raise ValueError("source telemetry must precede the snapshot time")
        # Flexible source attributes and JSON task schemas cannot smuggle generated
        # workflow rows, answers or vectors into the executable source package.
        forbidden = {
            "embedding",
            "embeddings",
            "evidence",
            "decision",
            "decisions",
            "approval",
            "approvals",
            "agent_execution",
            "agent_executions",
            "expected_modes",
            "expected_component",
            "review_expectations",
        }

        def inspect(value: object) -> None:
            if isinstance(value, dict):
                if any(str(key).casefold() in forbidden for key in value):
                    raise ValueError("input contains a generated or label-only field")
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(self.facts.model_dump(mode="json"))
        return self


class WorkflowInputBundle(SourceRecord):
    schema_version: Literal["openvigil.workflow-eval-input.v1"]
    created_at: AwareDatetime
    provenance: str = Field(min_length=10, max_length=2000)
    cases: list[WorkflowCaseInput] = Field(min_length=1, max_length=128)
    _raw_sha256: str | None = PrivateAttr(default=None)

    @property
    def input_bundle_sha256(self) -> str:
        if self._raw_sha256 is None:
            raise ValueError("load_input_bundle is required to establish raw byte identity")
        return self._raw_sha256

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        if len({case.case_id.casefold() for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate case identity")
        return self


class ReviewExpectation(SourceRecord):
    review_type: ReviewType
    allowed_outcomes: list[Literal["pass", "conditional_pass", "fail"]] = Field(
        min_length=1, max_length=3
    )
    required_conditions: list[str] = Field(default_factory=list, max_length=20)


class LabelProvenance(SourceRecord):
    kind: Literal["synthetic_fixture", "expert_review_declared"]
    source_summary: str = Field(min_length=10, max_length=2000)
    reviewed_by: str | None = Field(default=None, min_length=3, max_length=160)
    reviewed_at: AwareDatetime | None = None
    source_artifact_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def complete_declaration(self) -> Self:
        if self.kind == "expert_review_declared" and (
            self.reviewed_by is None
            or self.reviewed_at is None
            or self.source_artifact_sha256 is None
        ):
            raise ValueError("expert declaration requires reviewer, time and source digest")
        return self

    @property
    def expert_verified(self) -> bool:
        """An unverified declaration never confers qualification."""
        return False


class WorkflowCaseLabel(SourceRecord):
    case_id: Identifier
    input_case_sha256: Sha256
    expected_component: str = Field(min_length=2, max_length=80)
    expected_modes: list[str] = Field(min_length=1, max_length=20)
    abstain: bool
    expected_stop: Literal["human_review", "diagnosis_gate"]
    supporting_source_refs: list[str] = Field(default_factory=list, max_length=256)
    required_source_refs: list[str] = Field(default_factory=list, max_length=256)
    forbidden_source_refs: list[str] = Field(default_factory=list, max_length=256)
    allowed_execution_plan_ids: list[str] = Field(default_factory=list, max_length=20)
    # Literal casefold substring checks only; this is not semantic entailment.
    forbidden_public_phrases: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        default_factory=list, max_length=20
    )
    review_expectations: list[ReviewExpectation] = Field(default_factory=list, max_length=5)
    provenance: LabelProvenance

    @model_validator(mode="after")
    def consistent_expectations(self) -> Self:
        required = set(self.required_source_refs)
        supporting = set(self.supporting_source_refs)
        forbidden = set(self.forbidden_source_refs)
        if not required <= supporting or (supporting & forbidden):
            raise ValueError("source support labels conflict")
        if self.abstain:
            if self.expected_stop != "diagnosis_gate" or self.expected_modes != [
                "insufficient_evidence"
            ]:
                raise ValueError("abstention must preserve the production diagnosis gate")
            if self.review_expectations or self.allowed_execution_plan_ids:
                raise ValueError("abstention cannot claim downstream reviews or execution")
        elif self.expected_stop != "human_review" or {
            review.review_type for review in self.review_expectations
        } != {"engineering", "safety", "economic", "resource", "compliance"}:
            raise ValueError("human review cases require all five review expectations")
        return self


class WorkflowLabelBundle(SourceRecord):
    schema_version: Literal["openvigil.workflow-eval-label.v1"]
    input_bundle_sha256: Sha256
    cases: list[WorkflowCaseLabel] = Field(min_length=1, max_length=128)

    def verify_inputs(self, inputs: WorkflowInputBundle) -> None:
        if self.input_bundle_sha256 != inputs.input_bundle_sha256:
            raise ValueError("label input bundle SHA-256 mismatch")
        labels = {case.case_id: case for case in self.cases}
        if len(labels) != len(self.cases) or set(labels) != {case.case_id for case in inputs.cases}:
            raise ValueError("labels must preserve every input case exactly once")
        for case in inputs.cases:
            label = labels[case.case_id]
            if label.input_case_sha256 != canonical_sha256(case):
                raise ValueError("label input case SHA-256 mismatch")
            if label.expected_component != case.facts.mission_request.analysis_profile.component:
                raise ValueError("label component differs from the requested analysis scope")


def load_label_bundle(path: Path, inputs: WorkflowInputBundle) -> WorkflowLabelBundle:
    labels = WorkflowLabelBundle.model_validate_json(read_bundle_bytes(path))
    labels.verify_inputs(inputs)
    return labels
