"""Full-scale evaluation orchestration and registration, with compatible explicit exports."""

from __future__ import annotations

import hashlib as hashlib
import importlib as importlib
import importlib.metadata
import json as json
import math as math
import time as time
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass
from datetime import datetime as datetime
from pathlib import Path as Path
from pathlib import PurePosixPath as PurePosixPath
from typing import Any as Any
from typing import cast as cast

from sqlalchemy.ext.asyncio import AsyncSession as AsyncSession

import windops_backend.benchmarks.care.fullscale_import as _fullscale_import
from windops_backend.benchmarks.care.anomaly import (
    canonical_anomaly_input_schema as canonical_anomaly_input_schema,
)
from windops_backend.benchmarks.care.anomaly import (
    canonical_anomaly_output_schema as canonical_anomaly_output_schema,
)
from windops_backend.benchmarks.care.contract import DATASET_ID as DATASET_ID
from windops_backend.benchmarks.care.contract import DATASET_VERSION as DATASET_VERSION
from windops_backend.benchmarks.care.contract import CareContractError as CareContractError
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _accumulate_moments as _accumulate_moments,
)
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _build_fold_profiles as _build_fold_profiles,
)
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _build_prediction as _build_prediction,
)
from windops_backend.benchmarks.care.fullscale_evaluation_compute import _model_id as _model_id
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _model_selection as _model_selection,
)
from windops_backend.benchmarks.care.fullscale_evaluation_compute import _Moments as _Moments
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _new_moments as _new_moments,
)
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _quality_mask_impact_summary as _quality_mask_impact_summary,
)
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _threshold_policy as _threshold_policy,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    _evaluate_resource_gate as _evaluate_resource_gate,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    _operational_policy as _operational_policy,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    _verify_model_package as _verify_model_package,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    _verify_operational_policy as _verify_operational_policy,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    resource_prediction_count as resource_prediction_count,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    verify_full_scale_evaluation_manifest as verify_full_scale_evaluation_manifest,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    verify_full_scale_prediction_model_inputs as verify_full_scale_prediction_model_inputs,
)
from windops_backend.benchmarks.care.fullscale_evaluation_evidence import (
    verify_full_scale_prediction_status_evidence as verify_full_scale_prediction_status_evidence,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _batch_status_evidence as _batch_status_evidence,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _batch_values as _batch_values,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _evaluation_column_contracts as _evaluation_column_contracts,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _feature_matrix as _feature_matrix,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _iter_event_batches as _iter_event_batches,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _load_json_reference as _load_json_reference,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _load_quality_mask_artifact as _load_quality_mask_artifact,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _normalise_status as _normalise_status,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _quality_mask_index as _quality_mask_index,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _quality_mask_lineage as _quality_mask_lineage,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _quality_mask_matrix as _quality_mask_matrix,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _quality_mask_policy as _quality_mask_policy,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _resolve_reference as _resolve_reference,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _timestamp_text as _timestamp_text,
)
from windops_backend.benchmarks.care.importer import TRUTH_ACCESS_SCOPE as TRUTH_ACCESS_SCOPE
from windops_backend.benchmarks.care.importer import _artifact_reference as _artifact_reference
from windops_backend.benchmarks.care.importer import _sha256_file as _sha256_file
from windops_backend.benchmarks.care.importer import (
    _write_immutable_bytes as _write_immutable_bytes,
)
from windops_backend.benchmarks.care.licensing import (
    build_care_artifact_license as build_care_artifact_license,
)
from windops_backend.benchmarks.care.pipeline import FEATURE_SET_VERSION as FEATURE_SET_VERSION
from windops_backend.benchmarks.care.pipeline import QUALITY_RULE_VERSION as QUALITY_RULE_VERSION
from windops_backend.benchmarks.care.pipeline import BenchmarkJobCancelled as BenchmarkJobCancelled
from windops_backend.benchmarks.care.pipeline import BenchmarkJobState as BenchmarkJobState
from windops_backend.benchmarks.care.pipeline import (
    CareObjectStorageLayout as CareObjectStorageLayout,
)
from windops_backend.benchmarks.care.pipeline import (
    FileBenchmarkJobControl as FileBenchmarkJobControl,
)
from windops_backend.benchmarks.care.pipeline import (
    ImmutableArtifactStore as ImmutableArtifactStore,
)
from windops_backend.benchmarks.care.quality import (
    STATUS_CORROBORATION_RULE_ID as STATUS_CORROBORATION_RULE_ID,
)
from windops_backend.benchmarks.care.quality import ZERO_RUN_RULE_ID as ZERO_RUN_RULE_ID
from windops_backend.benchmarks.care.quality import StatusPointEvidence as StatusPointEvidence
from windops_backend.benchmarks.care.quality import StatusSequenceState as StatusSequenceState
from windops_backend.benchmarks.care.quality import evaluate_status_point as evaluate_status_point
from windops_backend.benchmarks.care.quality import status_evidence_policy as status_evidence_policy
from windops_backend.benchmarks.care.resources import (
    peak_process_resident_bytes as peak_process_resident_bytes,
)
from windops_backend.benchmarks.care.resources import (
    trim_process_resident_memory as trim_process_resident_memory,
)
from windops_backend.benchmarks.care.scoring import SCORE_PROTOCOL_VERSION as SCORE_PROTOCOL_VERSION
from windops_backend.benchmarks.care.scoring import CareEventTruth as CareEventTruth
from windops_backend.benchmarks.care.scoring import EvaluationRunSpec as EvaluationRunSpec
from windops_backend.benchmarks.care.scoring import FinalCareEvaluator as FinalCareEvaluator
from windops_backend.benchmarks.care.scoring import (
    ModelSelectionProvenance as ModelSelectionProvenance,
)
from windops_backend.benchmarks.care.scoring import PredictionBatch as PredictionBatch
from windops_backend.benchmarks.care.scoring import PredictionPoint as PredictionPoint
from windops_backend.benchmarks.care.scoring import PredictionTruthVault as PredictionTruthVault
from windops_backend.benchmarks.care.scoring import ThresholdPolicy as ThresholdPolicy
from windops_backend.benchmarks.care.scoring import (
    build_evaluation_artifact as build_evaluation_artifact,
)
from windops_backend.benchmarks.care.scoring import (
    build_prediction_artifact as build_prediction_artifact,
)
from windops_backend.benchmarks.care.scoring import (
    verify_prediction_artifact as verify_prediction_artifact,
)
from windops_backend.benchmarks.care.trust import CareTrustAnchor as CareTrustAnchor
from windops_backend.benchmarks.care.trust import (
    verify_embedded_care_approval as verify_embedded_care_approval,
)
from windops_backend.schemas import (
    BenchmarkEvaluationRunCreateRequest as BenchmarkEvaluationRunCreateRequest,
)
from windops_backend.schemas import (
    BenchmarkEventResultCreateRequest as BenchmarkEventResultCreateRequest,
)
from windops_backend.schemas import (
    BenchmarkMetricSnapshotCreateRequest as BenchmarkMetricSnapshotCreateRequest,
)
from windops_backend.schemas import ModelRegisterRequest as ModelRegisterRequest
from windops_backend.services.benchmark_metadata import (
    complete_evaluation_run as complete_evaluation_run,
)
from windops_backend.services.benchmark_metadata import (
    get_or_create_evaluation_run as get_or_create_evaluation_run,
)
from windops_backend.services.benchmark_metadata import record_event_result as record_event_result
from windops_backend.services.benchmark_metadata import (
    record_metric_snapshot as record_metric_snapshot,
)
from windops_backend.services.models import register_model as register_model

FULL_SCALE_IMPORT_VERSION = _fullscale_import.FULL_SCALE_IMPORT_VERSION
FULL_SCALE_IMPORT_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_IMPORT_SCHEMA_VERSION
FULL_SCALE_EVENT_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_EVENT_SCHEMA_VERSION
FULL_SCALE_EVALUATION_VERSION = _fullscale_import.FULL_SCALE_EVALUATION_VERSION
FULL_SCALE_EVALUATION_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_EVALUATION_SCHEMA_VERSION
FULL_SCALE_FOLD_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_FOLD_SCHEMA_VERSION
FULL_SCALE_MODEL_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_MODEL_SCHEMA_VERSION
FULL_SCALE_PREDICTION_FREEZE_SCHEMA_VERSION = (
    _fullscale_import.FULL_SCALE_PREDICTION_FREEZE_SCHEMA_VERSION
)
FULL_SCALE_RESOURCE_POLICY_VERSION = _fullscale_import.FULL_SCALE_RESOURCE_POLICY_VERSION
FULL_SCALE_OPERATIONAL_POLICY_VERSION = _fullscale_import.FULL_SCALE_OPERATIONAL_POLICY_VERSION
FULL_SCALE_PROTOCOL_FROZEN_AT = _fullscale_import.FULL_SCALE_PROTOCOL_FROZEN_AT
FULL_SCALE_THRESHOLD = _fullscale_import.FULL_SCALE_THRESHOLD
FULL_SCALE_THRESHOLD_VERSION = _fullscale_import.FULL_SCALE_THRESHOLD_VERSION
FULL_SCALE_THRESHOLD_RUN_ID = _fullscale_import.FULL_SCALE_THRESHOLD_RUN_ID
FULL_SCALE_MODEL_VERSION = _fullscale_import.FULL_SCALE_MODEL_VERSION
FULL_SCALE_QUALITY_MASK_POLICY_VERSION = _fullscale_import.FULL_SCALE_QUALITY_MASK_POLICY_VERSION
FULL_SCALE_TRAIN_STATUS_IDS = _fullscale_import.FULL_SCALE_TRAIN_STATUS_IDS
FULL_SCALE_STAGE_ORDER = _fullscale_import.FULL_SCALE_STAGE_ORDER
MIN_REPLAY_WRITE_ROWS_PER_SECOND = _fullscale_import.MIN_REPLAY_WRITE_ROWS_PER_SECOND
CareFullScaleError = _fullscale_import.CareFullScaleError
CareFullScaleResourceError = _fullscale_import.CareFullScaleResourceError
FullScaleExpectedCounts = _fullscale_import.FullScaleExpectedCounts
CARE_V6_FULL_SCALE_COUNTS = _fullscale_import.CARE_V6_FULL_SCALE_COUNTS
FullScaleResourceLimits = _fullscale_import.FullScaleResourceLimits
DEFAULT_FULL_SCALE_RESOURCE_LIMITS = _fullscale_import.DEFAULT_FULL_SCALE_RESOURCE_LIMITS
_FarmContract = _fullscale_import._FarmContract
_FullScaleContract = _fullscale_import._FullScaleContract
FullScaleImportResult = _fullscale_import.FullScaleImportResult
FullScaleEvaluationResult = _fullscale_import.FullScaleEvaluationResult
FullScaleImportRegistration = _fullscale_import.FullScaleImportRegistration
FullScaleEvaluationRegistration = _fullscale_import.FullScaleEvaluationRegistration
_canonical_bytes = _fullscale_import._canonical_bytes
_canonical_hash = _fullscale_import._canonical_hash
_resolved_artifact_root = _fullscale_import._resolved_artifact_root
_is_sha256 = _fullscale_import._is_sha256
_safe_token = _fullscale_import._safe_token
_license = _fullscale_import._license
_verify_license = _fullscale_import._verify_license
_build_contract = _fullscale_import._build_contract
build_full_scale_plan = _fullscale_import.build_full_scale_plan
_event_summary_path = _fullscale_import._event_summary_path
_verify_event_summary = _fullscale_import._verify_event_summary
_load_existing_event_summary = _fullscale_import._load_existing_event_summary
_resource_max = _fullscale_import._resource_max
_audit_with_process_resources = _fullscale_import._audit_with_process_resources
_enforce_import_resources = _fullscale_import._enforce_import_resources
build_full_scale_import = _fullscale_import.build_full_scale_import
verify_full_scale_import_manifest = _fullscale_import.verify_full_scale_import_manifest
register_full_scale_import = _fullscale_import.register_full_scale_import


def _publish_self_hashed_json(
    *,
    output_root: Path,
    path: Path,
    document: Mapping[str, Any],
    document_sha256: str,
    object_key_prefix: str,
    artifact_store: ImmutableArtifactStore | None,
) -> dict[str, Any]:
    content = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    _write_immutable_bytes(path, content)
    file_sha256 = hashlib.sha256(content).hexdigest()
    object_key = f"{object_key_prefix}.sha256-{file_sha256}.json"
    artifact_uri = (
        artifact_store.put_immutable(
            object_key,
            path,
            file_sha256,
            content_type="application/json",
        )
        if artifact_store is not None
        else path.resolve().as_uri()
    )
    return _artifact_reference(
        output_root=output_root,
        path=path,
        artifact_uri=artifact_uri,
        file_sha256=file_sha256,
        document_sha256=document_sha256,
    )


def _truth_from_event(output_root: Path, event: Mapping[str, Any]) -> CareEventTruth:
    raw = _load_json_reference(output_root, event["restricted_truth"]["artifact"])
    if (
        raw.get("access_scope") != TRUTH_ACCESS_SCOPE
        or raw.get("model_input_allowed") is not False
        or raw.get("event_id") != event["event_id"]
        or raw.get("farm") != event["farm"]
        or str(raw.get("source_asset_id")) != str(event["source_asset_id"])
    ):
        raise CareFullScaleError("restricted truth identity or access contract is invalid")
    description = str(raw.get("event_description", "")).strip()
    return CareEventTruth(
        event_id=int(raw["event_id"]),
        farm=str(raw["farm"]),
        source_asset_id=str(raw["source_asset_id"]),
        event_label=str(raw["event_label"]),
        event_start_source_row_id=int(raw["event_start_id"]),
        event_end_source_row_id=int(raw["event_end_id"]),
        failure_type=description or None,
    )


def build_full_scale_evaluation(
    import_manifest: Mapping[str, Any],
    output_root: Path,
    *,
    artifact_store: ImmutableArtifactStore | None = None,
    job_id: str = "care-v6-full-evaluation",
    state_path: Path | None = None,
    stop_after_predictions: int | None = None,
    expected_counts: FullScaleExpectedCounts = CARE_V6_FULL_SCALE_COUNTS,
    resource_limits: FullScaleResourceLimits = DEFAULT_FULL_SCALE_RESOURCE_LIMITS,
    trust_anchor: CareTrustAnchor | None = None,
) -> FullScaleEvaluationResult:
    """Run two bounded passes and evaluate one held-out-asset fold per farm asset."""

    approval = import_manifest.get("care_approval")
    if not isinstance(approval, Mapping):
        raise CareFullScaleError("full-scale import is missing CARE approved-root lineage")
    try:
        verify_embedded_care_approval(approval, trust_anchor=trust_anchor)
    except CareContractError as exc:
        raise CareFullScaleError(str(exc)) from exc
    if approval.get("source_manifest_sha256") != import_manifest.get(
        "source_manifest_sha256"
    ) or approval.get("quality_contract_sha256") != import_manifest.get("quality_contract_sha256"):
        raise CareFullScaleError("full-scale import approved-root lineage hashes drifted")

    output_root = output_root.resolve()
    manifest_path = (
        output_root / DATASET_ID / DATASET_VERSION / "reports" / "full-evaluation" / "manifest.json"
    )
    if manifest_path.exists():
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise CareFullScaleError("full-scale evaluation manifest root must be an object")
        verify_full_scale_evaluation_manifest(
            raw,
            output_root=output_root,
            import_manifest=import_manifest,
            expected_counts=expected_counts,
            resource_limits=resource_limits,
            trust_anchor=trust_anchor,
        )
        return FullScaleEvaluationResult(manifest_path, raw, _sha256_file(manifest_path), True)
    if (
        import_manifest.get("schema_version") != FULL_SCALE_IMPORT_SCHEMA_VERSION
        or import_manifest.get("summary") != expected_counts.to_document()
        or import_manifest.get("raw_source_unchanged") is not True
    ):
        raise CareFullScaleError("full-scale evaluation requires a verified import manifest")
    source_dataset_sha256 = str(import_manifest["source_dataset_sha256"])
    quality_contract_sha256 = str(import_manifest["quality_contract_sha256"])
    if not _is_sha256(source_dataset_sha256) or not _is_sha256(quality_contract_sha256):
        raise CareFullScaleError("full-scale evaluation control hashes are invalid")
    features_by_farm, status_signals_by_farm = _evaluation_column_contracts(
        output_root,
        import_manifest,
    )

    state_file = state_path or output_root / ".state" / f"{job_id}.json"
    job_control = FileBenchmarkJobControl(
        state_file,
        BenchmarkJobState.pending(job_id, "evaluate"),
    )
    state = job_control.start_attempt()
    if state.status == "cancelled":
        raise BenchmarkJobCancelled("full-scale evaluation was cancelled before restart")
    layout = CareObjectStorageLayout()
    started = time.perf_counter()
    try:
        totals, asset_moments, training_resources = _accumulate_moments(
            import_manifest=import_manifest,
            output_root=output_root,
            features_by_farm=features_by_farm,
            status_signals_by_farm=status_signals_by_farm,
        )
        profiles = _build_fold_profiles(
            totals=totals,
            assets=asset_moments,
            features_by_farm=features_by_farm,
            import_manifest=import_manifest,
        )
        if len(profiles) != expected_counts.asset_count:
            raise CareFullScaleError("full-scale fold count differs from farm-namespaced assets")

        model_references: dict[str, Mapping[str, Any]] = {}
        for farm in FULL_SCALE_STAGE_ORDER:
            farm_profiles = [
                profiles[(profile_farm, asset)]
                for profile_farm, asset in sorted(profiles)
                if profile_farm == farm
            ]
            quality_mask_policy = _quality_mask_policy(features_by_farm[farm])
            quality_mask_lineage = _quality_mask_lineage(import_manifest, farm)
            model_payload = {
                "schema_version": FULL_SCALE_MODEL_SCHEMA_VERSION,
                "dataset_id": DATASET_ID,
                "dataset_version": DATASET_VERSION,
                "farm": farm,
                "model_id": _model_id(farm),
                "model_version": FULL_SCALE_MODEL_VERSION,
                "model_kind": "anomaly",
                "algorithm": "maximum-absolute-leave-one-asset-standard-score",
                "generalization_protocol": "within-farm-leave-one-turbine-out-v1",
                "feature_set_version": FEATURE_SET_VERSION,
                "feature_set_sha256": _canonical_hash(list(features_by_farm[farm])),
                "feature_columns": list(features_by_farm[farm]),
                "quality_rule_version": QUALITY_RULE_VERSION,
                "quality_contract_sha256": quality_contract_sha256,
                "quality_mask_policy": quality_mask_policy,
                "quality_mask_lineage": quality_mask_lineage,
                "care_approval": dict(approval),
                "threshold_policy": _threshold_policy().to_dict(),
                "fold_profiles": farm_profiles,
                "dependency_identity": {
                    "implementation": FULL_SCALE_EVALUATION_VERSION,
                    "numpy": importlib.metadata.version("numpy"),
                    "pyarrow": importlib.metadata.version("pyarrow"),
                    "python_numeric_contract": "float64-streaming-sum-sumsq-v1",
                },
                "training_split": "train",
                "trusted_train_status_ids": list(FULL_SCALE_TRAIN_STATUS_IDS),
                "status_evidence_policy": status_evidence_policy(
                    farm,
                    status_signals_by_farm[farm],
                ),
                "prediction_truth_used": False,
                "cross_farm_ontology_used": False,
                "license": build_care_artifact_license(
                    artifact_type="full-scale-anomaly-model-package",
                    changes_made=(
                        "Computed per-farm leave-one-asset-out means and standard deviations "
                        "from status-usable train rows after applying immutable feature masks; "
                        "no prediction truth was read."
                    ),
                    source_dataset_sha256=source_dataset_sha256,
                    source_artifact_sha256=str(import_manifest["manifest_sha256"]),
                    transformation_versions={
                        "evaluation": FULL_SCALE_EVALUATION_VERSION,
                        "feature_set": FEATURE_SET_VERSION,
                        "quality_rules": QUALITY_RULE_VERSION,
                        "score_protocol": SCORE_PROTOCOL_VERSION,
                    },
                ),
            }
            model = {**model_payload, "model_package_sha256": _canonical_hash(model_payload)}
            _verify_model_package(model)
            model_path = (
                output_root
                / DATASET_ID
                / DATASET_VERSION
                / "reports"
                / "full-evaluation"
                / "models"
                / f"farm={farm}"
                / "model.json"
            )
            model_references[farm] = _publish_self_hashed_json(
                output_root=output_root,
                path=model_path,
                document=model,
                document_sha256=str(model["model_package_sha256"]),
                object_key_prefix=(
                    f"{layout.reports_prefix}/full-evaluation/models/farm={farm}/model"
                ),
                artifact_store=artifact_store,
            )

        prediction_references: dict[str, Mapping[str, Any]] = {}
        prediction_resources: list[Mapping[str, Any]] = []
        completed_predictions = 0
        for event in import_manifest["events"]:
            job_control.cancellation_point()
            farm = str(event["farm"])
            asset = str(event["source_asset_id"])
            event_id = int(event["event_id"])
            prediction_path = (
                output_root
                / DATASET_ID
                / DATASET_VERSION
                / "predictions"
                / "full-evaluation"
                / f"model={_model_id(farm)}"
                / f"farm={farm}"
                / f"event={event_id}.json"
            )
            existing_reference: Mapping[str, Any] | None = None
            if prediction_path.exists():
                raw_prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
                if not isinstance(raw_prediction, Mapping):
                    raise CareFullScaleError("full-scale prediction root must be an object")
                verify_full_scale_prediction_model_inputs(
                    raw_prediction,
                    output_root=output_root,
                    event=event,
                    features=features_by_farm[farm],
                    status_signal_columns=status_signals_by_farm[farm],
                    profile=profiles[(farm, asset)],
                )
                if (
                    raw_prediction.get("event_id") != event_id
                    or raw_prediction.get("farm") != farm
                    or str(raw_prediction.get("source_asset_id")) != asset
                    or raw_prediction.get("model_id") != _model_id(farm)
                ):
                    raise CareFullScaleError("existing full-scale prediction identity drifted")
                existing_reference = _artifact_reference(
                    output_root=output_root,
                    path=prediction_path,
                    artifact_uri=prediction_path.resolve().as_uri(),
                    file_sha256=_sha256_file(prediction_path),
                    document_sha256=str(raw_prediction["prediction_artifact_sha256"]),
                )
                resources = {
                    "elapsed_seconds": 0.0,
                    "prediction_point_count": int(raw_prediction["point_count"]),
                    "peak_record_batch_bytes": 0,
                    "peak_arrow_allocated_bytes": 0,
                    "resumed": True,
                }
            else:
                prediction, resources = _build_prediction(
                    event=event,
                    output_root=output_root,
                    features=features_by_farm[farm],
                    status_signal_columns=status_signals_by_farm[farm],
                    profile=profiles[(farm, asset)],
                    feature_set_sha256=_canonical_hash(list(features_by_farm[farm])),
                    quality_contract_sha256=quality_contract_sha256,
                    approval_lineage=approval,
                    license_metadata=build_care_artifact_license(
                        artifact_type="full-scale-event-prediction",
                        changes_made=(
                            "Applied the held-out-asset farm profile to prediction rows and "
                            "saved scores, binary decisions, status masks, and criticality."
                        ),
                        source_dataset_sha256=source_dataset_sha256,
                        source_artifact_sha256=str(event["parquet"]["data"]["file_sha256"]),
                        transformation_versions={
                            "evaluation": FULL_SCALE_EVALUATION_VERSION,
                            "feature_set": FEATURE_SET_VERSION,
                            "quality_rules": QUALITY_RULE_VERSION,
                            "score_protocol": SCORE_PROTOCOL_VERSION,
                        },
                    ),
                )
                existing_reference = _publish_self_hashed_json(
                    output_root=output_root,
                    path=prediction_path,
                    document=prediction,
                    document_sha256=str(prediction["prediction_artifact_sha256"]),
                    object_key_prefix=(
                        f"{layout.predictions_prefix}/full-evaluation/model={_model_id(farm)}/farm={farm}/event={event_id}/prediction"
                    ),
                    artifact_store=artifact_store,
                )
                completed_predictions += 1
            prediction_references[str(event_id)] = existing_reference
            prediction_resources.append({"event_id": event_id, **resources})
            job_control.heartbeat(
                completed=len(prediction_references),
                total=len(import_manifest["events"]),
                current_file=str(prediction_path),
                checkpoint_path=str(prediction_path),
                resource_stats={
                    "prediction_event_count": len(prediction_references),
                    "prediction_point_count": sum(
                        int(item["prediction_point_count"]) for item in prediction_resources
                    ),
                },
            )
            if (
                stop_after_predictions is not None
                and completed_predictions >= stop_after_predictions
            ):
                job_control.request_cancel()
                job_control.cancellation_point()

        freeze_payload = {
            "schema_version": FULL_SCALE_PREDICTION_FREEZE_SCHEMA_VERSION,
            "dataset_id": DATASET_ID,
            "dataset_version": DATASET_VERSION,
            "source_import_manifest_sha256": import_manifest["manifest_sha256"],
            "care_approval": dict(approval),
            "prediction_truth_present": False,
            "prediction_truth_read": False,
            "quality_mask_policy_version": FULL_SCALE_QUALITY_MASK_POLICY_VERSION,
            "quality_masks_applied": True,
            "event_ids": [int(event["event_id"]) for event in import_manifest["events"]],
            "prediction_artifacts": prediction_references,
            "prediction_event_count": len(prediction_references),
            "prediction_point_count": sum(
                int(item["prediction_point_count"]) for item in prediction_resources
            ),
        }
        freeze = {**freeze_payload, "freeze_sha256": _canonical_hash(freeze_payload)}
        freeze_path = (
            output_root
            / DATASET_ID
            / DATASET_VERSION
            / "reports"
            / "full-evaluation"
            / "prediction-freeze.json"
        )
        freeze_reference = _publish_self_hashed_json(
            output_root=output_root,
            path=freeze_path,
            document=freeze,
            document_sha256=str(freeze["freeze_sha256"]),
            object_key_prefix=f"{layout.reports_prefix}/full-evaluation/prediction-freeze",
            artifact_store=artifact_store,
        )

        # Truth is intentionally opened only after all truth-free predictions are frozen.
        truths = {
            int(event["event_id"]): _truth_from_event(output_root, event)
            for event in import_manifest["events"]
        }
        fold_references: list[Mapping[str, Any]] = []
        fold_summaries: list[Mapping[str, Any]] = []
        accounted_events: set[int] = set()
        for farm, asset in sorted(profiles):
            fold_events = [
                event
                for event in import_manifest["events"]
                if event["farm"] == farm and str(event["source_asset_id"]) == asset
            ]
            event_ids = tuple(int(event["event_id"]) for event in fold_events)
            predictions = [
                _load_json_reference(output_root, prediction_references[str(event_id)])
                for event_id in event_ids
            ]
            evaluator = FinalCareEvaluator(
                PredictionTruthVault(tuple(truths[event_id] for event_id in event_ids))
            )
            run = EvaluationRunSpec(
                run_id=(f"care-v6-{farm.lower()}-loao-{_safe_token(asset)}-final-v2"),
                purpose="final-holdout",
                generalization_protocol="within-farm-leave-one-turbine-out-v1",
                event_ids=event_ids,
                model_id=_model_id(farm),
                model_version=FULL_SCALE_MODEL_VERSION,
                created_at=FULL_SCALE_PROTOCOL_FROZEN_AT,
                farm=farm,
                held_out_asset_id=asset,
            )
            evaluation = build_evaluation_artifact(
                run,
                evaluator,
                predictions,
                approval_lineage=approval,
            )
            fold_profile = profiles[(farm, asset)]
            prediction_quality_masks = [
                {
                    "event_id": int(prediction["event_id"]),
                    "quality_mask_file_sha256": prediction["quality_mask_reference"]["file_sha256"],
                    "quality_mask_range_count": prediction["quality_mask_range_count"],
                    "quality_masked_prediction_feature_value_count": prediction[
                        "quality_masked_prediction_feature_value_count"
                    ],
                    "quality_masked_prediction_point_count": prediction[
                        "quality_masked_prediction_point_count"
                    ],
                }
                for prediction in predictions
            ]
            fold_payload = {
                "schema_version": FULL_SCALE_FOLD_SCHEMA_VERSION,
                "dataset_id": DATASET_ID,
                "dataset_version": DATASET_VERSION,
                "farm": farm,
                "held_out_asset_id": asset,
                "generalization_protocol": "within-farm-leave-one-turbine-out-v1",
                "prediction_freeze_sha256": freeze["freeze_sha256"],
                "truth_read_after_prediction_freeze": True,
                "model_package_sha256": model_references[farm]["document_sha256"],
                "quality_mask_policy": _quality_mask_policy(features_by_farm[farm]),
                "fold_profile": dict(fold_profile),
                "prediction_quality_masks": prediction_quality_masks,
                "evaluation": evaluation,
                "care_approval": dict(approval),
                "license": build_care_artifact_license(
                    artifact_type="within-farm-evaluation-fold",
                    changes_made=(
                        "Evaluated one farm-namespaced held-out asset after every prediction "
                        "artifact was frozen; training and prediction mask lineage was retained, "
                        "and truth was unavailable to training and calibration."
                    ),
                    source_dataset_sha256=source_dataset_sha256,
                    source_artifact_sha256=str(freeze["freeze_sha256"]),
                    transformation_versions={
                        "evaluation": FULL_SCALE_EVALUATION_VERSION,
                        "feature_set": FEATURE_SET_VERSION,
                        "quality_rules": QUALITY_RULE_VERSION,
                        "score_protocol": SCORE_PROTOCOL_VERSION,
                    },
                ),
            }
            fold = {**fold_payload, "document_sha256": _canonical_hash(fold_payload)}
            fold_path = (
                output_root
                / DATASET_ID
                / DATASET_VERSION
                / "reports"
                / "full-evaluation"
                / "folds"
                / f"farm={farm}"
                / f"held-out-asset={_safe_token(asset)}.json"
            )
            fold_reference = _publish_self_hashed_json(
                output_root=output_root,
                path=fold_path,
                document=fold,
                document_sha256=str(fold["document_sha256"]),
                object_key_prefix=(
                    f"{layout.reports_prefix}/full-evaluation/folds/farm={farm}/held-out-asset={_safe_token(asset)}"
                ),
                artifact_store=artifact_store,
            )
            fold_references.append(fold_reference)
            summary = evaluation["summary"]
            fold_summaries.append(
                {
                    "farm": farm,
                    "held_out_asset_id": asset,
                    "run_id": run.run_id,
                    "event_ids": list(event_ids),
                    "requested_event_count": summary["requested_event_count"],
                    "scored_event_count": summary["scored_event_count"],
                    "unscorable_event_count": summary["unscorable_event_count"],
                    "data_failure_event_count": summary["data_failure_event_count"],
                    "model_failure_event_count": summary["model_failure_event_count"],
                    "care_score": summary.get("care_score"),
                    "normal_event_false_positive_rate": summary.get("event_metrics", {}).get(
                        "normal_event_false_positive_rate"
                    ),
                    "event_detection_rate": summary.get("event_metrics", {}).get(
                        "event_detection_rate"
                    ),
                    "release_candidate_passed": (
                        summary.get("status") == "scored"
                        and float(summary.get("care_score", 0.0)) >= 0.5
                        and int(summary.get("unscorable_event_count", 0)) == 0
                        and int(summary.get("data_failure_event_count", 0)) == 0
                        and int(summary.get("model_failure_event_count", 0)) == 0
                    ),
                }
            )
            if accounted_events.intersection(event_ids):
                raise CareFullScaleError("full-scale event was evaluated in more than one fold")
            accounted_events.update(event_ids)
        expected_event_ids = {int(event["event_id"]) for event in import_manifest["events"]}
        if accounted_events != expected_event_ids:
            raise CareFullScaleError("full-scale folds do not account for every event")

        peak_resident = peak_process_resident_bytes()
        evaluation_elapsed = time.perf_counter() - started
        storage_bytes = sum(
            int(reference["size_bytes"])
            for reference in [
                *model_references.values(),
                *prediction_references.values(),
                freeze_reference,
                *fold_references,
            ]
        )
        prediction_count = int(freeze["prediction_point_count"])
        peak_batch = max(
            int(training_resources["peak_record_batch_bytes"]),
            max(
                (int(item["peak_record_batch_bytes"]) for item in prediction_resources),
                default=0,
            ),
        )
        peak_arrow = max(
            int(training_resources["peak_arrow_allocated_bytes"]),
            max(
                (int(item["peak_arrow_allocated_bytes"]) for item in prediction_resources),
                default=0,
            ),
        )
        resource_actual = _evaluate_resource_gate(
            elapsed_seconds=evaluation_elapsed,
            storage_bytes=storage_bytes,
            prediction_count=prediction_count,
            peak_batch=peak_batch,
            peak_arrow=peak_arrow,
            peak_resident=peak_resident,
            limits=resource_limits,
        )
        candidate_pass_count = sum(
            bool(summary["release_candidate_passed"]) for summary in fold_summaries
        )
        quality_mask_impact = _quality_mask_impact_summary(
            training_resources,
            [
                _load_json_reference(output_root, prediction_references[str(event["event_id"])])
                for event in import_manifest["events"]
            ],
        )
        payload = {
            "schema_version": FULL_SCALE_EVALUATION_SCHEMA_VERSION,
            "dataset_id": DATASET_ID,
            "dataset_version": DATASET_VERSION,
            "source_import_manifest_sha256": import_manifest["manifest_sha256"],
            "source_dataset_sha256": source_dataset_sha256,
            "quality_contract_sha256": quality_contract_sha256,
            "care_approval": dict(approval),
            "generalization_protocol": "within-farm-leave-one-turbine-out-v1",
            "farm_namespaced_fold_count": len(profiles),
            "model_packages": model_references,
            "prediction_freeze": freeze_reference,
            "fold_artifacts": fold_references,
            "fold_summaries": fold_summaries,
            "summary": {
                **expected_counts.to_document(),
                "prediction_event_count": len(prediction_references),
                "prediction_point_count": prediction_count,
                "fold_count": len(profiles),
                "candidate_release_pass_count": candidate_pass_count,
                "candidate_release_fail_count": len(profiles) - candidate_pass_count,
                "all_events_accounted_for": True,
            },
            "resource_policy": resource_limits.to_document(),
            "resource_actual": resource_actual,
            "training_resources": training_resources,
            "prediction_resources": prediction_resources,
            "quality_mask_impact": quality_mask_impact,
            "operational_policy": _operational_policy(resource_limits),
            "server_release_gate": {
                "authority": "immutable-evaluation-run-and-metric-snapshot-only",
                "frontend_or_json_override_allowed": False,
                "failed_or_unscorable_events_may_be_omitted": False,
                "candidate_results_are_recorded_even_when_gate_fails": True,
            },
            "cross_farm_protocol": {
                "status": "disabled",
                "reason": "canonical ontology and recorded human review are not complete",
                "human_review_ids": [],
            },
            "truth_boundary": {
                "prediction_freeze_completed_before_truth_read": True,
                "prediction_truth_available_to_training_or_calibration": False,
            },
            "license": build_care_artifact_license(
                artifact_type="full-scale-evaluation-manifest",
                changes_made=(
                    "Ran streaming within-farm leave-one-asset-out evaluation for every "
                    "farm-namespaced asset and retained all scored, failed, and unscorable "
                    "outcomes."
                ),
                source_dataset_sha256=source_dataset_sha256,
                source_artifact_sha256=str(import_manifest["manifest_sha256"]),
                transformation_versions={
                    "evaluation": FULL_SCALE_EVALUATION_VERSION,
                    "feature_set": FEATURE_SET_VERSION,
                    "quality_rules": QUALITY_RULE_VERSION,
                    "score_protocol": SCORE_PROTOCOL_VERSION,
                },
            ),
        }
        manifest = {**payload, "manifest_sha256": _canonical_hash(payload)}
        content = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _write_immutable_bytes(manifest_path, content)
        verify_full_scale_evaluation_manifest(
            manifest,
            output_root=output_root,
            import_manifest=import_manifest,
            expected_counts=expected_counts,
            resource_limits=resource_limits,
            trust_anchor=trust_anchor,
        )
        job_control.complete(manifest_path.resolve().as_uri(), resource_actual)
        return FullScaleEvaluationResult(
            manifest_path,
            manifest,
            hashlib.sha256(content).hexdigest(),
            False,
        )
    except BenchmarkJobCancelled:
        raise
    except Exception as exc:
        job_control.fail(exc, retryable=not isinstance(exc, CareFullScaleResourceError))
        raise


async def register_full_scale_evaluation(
    session: AsyncSession,
    evaluation_manifest: Mapping[str, Any],
    import_manifest: Mapping[str, Any],
    *,
    artifact_root: Path,
    subject: str,
    expected_counts: FullScaleExpectedCounts = CARE_V6_FULL_SCALE_COUNTS,
    resource_limits: FullScaleResourceLimits = DEFAULT_FULL_SCALE_RESOURCE_LIMITS,
    trust_anchor: CareTrustAnchor | None = None,
) -> FullScaleEvaluationRegistration:
    """Register three farm models, every held-out fold, and every event outcome."""

    artifact_root = _resolved_artifact_root(artifact_root)
    verify_full_scale_evaluation_manifest(
        evaluation_manifest,
        output_root=artifact_root,
        import_manifest=import_manifest,
        expected_counts=expected_counts,
        resource_limits=resource_limits,
        trust_anchor=trust_anchor,
    )
    created = replayed = 0
    model_ids: list[str] = []
    evaluation_run_ids: list[str] = []
    model_rows: dict[str, Any] = {}
    model_packages: dict[str, Mapping[str, Any]] = {}
    for farm in FULL_SCALE_STAGE_ORDER:
        reference = evaluation_manifest["model_packages"][farm]
        package = _load_json_reference(artifact_root, reference)
        model_id = str(package["model_id"])
        model_ids.append(model_id)
        model, was_replayed = await register_model(
            session,
            ModelRegisterRequest(
                model_id=model_id,
                name=f"CARE {farm} within-farm LOAO z-score",
                version=str(package["model_version"]),
                kind="anomaly",
                description=(
                    "Deterministic CARE v6 within-farm leave-one-asset-out anomaly model; "
                    "prediction truth is excluded from training and threshold calibration."
                ),
                artifact_uri=str(reference["artifact_uri"]),
                artifact_sha256=str(reference["file_sha256"]),
                content_type="application/json",
                input_schema=canonical_anomaly_input_schema(),
                output_schema=canonical_anomaly_output_schema(),
                metrics={
                    "algorithm": package["algorithm"],
                    "evaluation_role": "full-scale-within-farm",
                    "generalization_protocol": package["generalization_protocol"],
                    "dependency_identity": package["dependency_identity"],
                    "feature_set_version": package["feature_set_version"],
                    "quality_rule_version": package["quality_rule_version"],
                    "quality_mask_policy": package["quality_mask_policy"],
                    "quality_mask_lineage_sha256": package["quality_mask_lineage"][
                        "lineage_sha256"
                    ],
                    "model_package_sha256": package["model_package_sha256"],
                    "prediction_truth_used": False,
                    "care_approval": dict(evaluation_manifest["care_approval"]),
                },
            ),
            content_size_bytes=int(reference["size_bytes"]),
            subject=subject,
        )
        created += 0 if was_replayed else 1
        replayed += 1 if was_replayed else 0
        model_rows[farm] = model
        model_packages[farm] = package

    freeze = _load_json_reference(artifact_root, evaluation_manifest["prediction_freeze"])
    prediction_references = freeze["prediction_artifacts"]
    summaries = {
        str(summary["run_id"]): summary for summary in evaluation_manifest["fold_summaries"]
    }
    completed_at = datetime.fromisoformat(FULL_SCALE_PROTOCOL_FROZEN_AT)
    for fold_reference in evaluation_manifest["fold_artifacts"]:
        fold = _load_json_reference(artifact_root, fold_reference)
        evaluation = fold["evaluation"]
        run_document = evaluation["run"]
        run_id = str(run_document["run_id"])
        farm = str(fold["farm"])
        summary = summaries[run_id]
        package = model_packages[farm]
        threshold = package["threshold_policy"]
        evaluation_run_ids.append(run_id)
        input_identity = _canonical_hash(
            {
                "source_import_manifest_sha256": import_manifest["manifest_sha256"],
                "prediction_freeze_sha256": freeze["freeze_sha256"],
                "model_package_sha256": package["model_package_sha256"],
                "farm": farm,
                "held_out_asset_id": fold["held_out_asset_id"],
                "event_ids": run_document["event_ids"],
                "care_approved_root_sha256": evaluation_manifest["care_approval"][
                    "approved_root_sha256"
                ],
            }
        )
        run, was_replayed = await get_or_create_evaluation_run(
            session,
            BenchmarkEvaluationRunCreateRequest(
                evaluation_run_id=run_id,
                dataset_version_id="care-v6",
                model_id=model_rows[farm].id,
                model_version=model_rows[farm].version,
                run_kind="final-holdout",
                protocol_version="within-farm-leave-one-turbine-out-v1",
                farm=cast(Any, farm),
                feature_set_version=str(package["feature_set_version"]),
                quality_rule_version=str(package["quality_rule_version"]),
                threshold_policy_version=str(threshold["version"]),
                threshold_policy_sha256=str(threshold["threshold_policy_sha256"]),
                random_seed=0,
                input_identity_sha256=input_identity,
                requested_event_count=int(summary["requested_event_count"]),
                extension_data={
                    "held_out_asset_id": fold["held_out_asset_id"],
                    "fold_artifact": fold_reference,
                    "model_package": evaluation_manifest["model_packages"][farm],
                    "prediction_freeze_sha256": freeze["freeze_sha256"],
                    "prediction_truth_used": False,
                    "quality_mask_policy": package["quality_mask_policy"],
                    "quality_mask_lineage_sha256": package["quality_mask_lineage"][
                        "lineage_sha256"
                    ],
                    "release_candidate_passed": summary["release_candidate_passed"],
                    "care_approval": dict(evaluation_manifest["care_approval"]),
                },
            ),
            subject=subject,
        )
        created += 0 if was_replayed else 1
        replayed += 1 if was_replayed else 0
        components = evaluation["summary"].get("components", {})
        if not isinstance(components, Mapping):
            components = {}
        for result in evaluation["event_results"]:
            event_id = int(result["event_id"])
            result_status = str(result["status"])
            scored = result_status == "scored"
            prediction = prediction_references.get(str(event_id))
            database_status = (
                "scored" if scored else "unscorable" if result_status == "unscorable" else "failed"
            )
            _, was_replayed = await record_event_result(
                session,
                BenchmarkEventResultCreateRequest(
                    event_result_id=f"{run_id}-e-{event_id}",
                    evaluation_run_id=run.id,
                    event_id=f"care-v6-event-{farm.lower()}-{event_id}",
                    status=cast(Any, database_status),
                    scorable=scored,
                    anomaly_detected=(bool(result["event_detected"]) if scored else None),
                    care_score=(
                        float(summary["care_score"])
                        if scored and summary.get("care_score") is not None
                        else None
                    ),
                    coverage_score=(
                        float(result["coverage_f0_5"])
                        if scored and result.get("coverage_f0_5") is not None
                        else None
                    ),
                    accuracy_score=(
                        float(result["point_accuracy"])
                        if scored and result.get("point_accuracy") is not None
                        else None
                    ),
                    reliability_score=(
                        float(components["reliability_event_f0_5"])
                        if scored and components.get("reliability_event_f0_5") is not None
                        else None
                    ),
                    earliness_score=(
                        float(result["earliness"])
                        if scored and result.get("earliness") is not None
                        else None
                    ),
                    prediction_artifact_uri=(
                        str(prediction["artifact_uri"]) if prediction is not None else None
                    ),
                    prediction_artifact_sha256=(
                        str(prediction["file_sha256"]) if prediction is not None else None
                    ),
                    failure_code=(
                        str(result.get("unscorable_reason") or result.get("failure_reason"))
                        if not scored
                        else None
                    ),
                    result_sha256=str(result["event_result_sha256"]),
                    details={
                        "event_label": result.get("event_label"),
                        "failure_type": result.get("failure_type"),
                        "care_score_scope": "fold-aggregate-or-null-for-single-class-fold",
                        "fold_artifact_uri": fold_reference["artifact_uri"],
                        "fold_artifact_sha256": fold_reference["file_sha256"],
                        "point_confusion": {
                            key: result.get(key) for key in ("tp", "fp", "tn", "fn")
                        },
                    },
                ),
            )
            created += 0 if was_replayed else 1
            replayed += 1 if was_replayed else 0

        metric_values: list[dict[str, Any]] = [
            {
                "name": "scored_event_count",
                "value": float(summary["scored_event_count"]),
                "unit": "events",
                "release": False,
            },
            *[
                {
                    "name": name,
                    "value": float(summary[name]),
                    "unit": "events",
                    "release": True,
                    "threshold": 0.0,
                    "direction": "eq",
                    "passed": int(summary[name]) == 0,
                }
                for name in (
                    "unscorable_event_count",
                    "data_failure_event_count",
                    "model_failure_event_count",
                )
            ],
            {
                "name": "release_candidate_pass",
                "value": float(bool(summary["release_candidate_passed"])),
                "unit": "boolean",
                "release": True,
                "threshold": 1.0,
                "direction": "eq",
                "passed": bool(summary["release_candidate_passed"]),
            },
        ]
        for name in (
            "care_score",
            "normal_event_false_positive_rate",
            "event_detection_rate",
        ):
            if summary.get(name) is not None:
                metric_values.append(
                    {
                        "name": name,
                        "value": float(summary[name]),
                        "unit": "ratio",
                        "release": name == "care_score",
                        "threshold": 0.5 if name == "care_score" else None,
                        "direction": "gte" if name == "care_score" else None,
                        "passed": (float(summary[name]) >= 0.5 if name == "care_score" else None),
                    }
                )
        for metric_index, metric in enumerate(metric_values):
            _, was_replayed = await record_metric_snapshot(
                session,
                BenchmarkMetricSnapshotCreateRequest(
                    metric_snapshot_id=f"{run_id}-m-{metric_index:02d}",
                    evaluation_run_id=run.id,
                    metric_name=str(metric["name"]),
                    protocol_version=SCORE_PROTOCOL_VERSION,
                    metric_version="care-v6-full-scale-fold-v1",
                    value=float(metric["value"]),
                    unit=str(metric["unit"]),
                    is_release_metric=bool(metric["release"]),
                    threshold_value=metric.get("threshold"),
                    threshold_direction=cast(Any, metric.get("direction")),
                    passed=metric.get("passed"),
                    details={
                        "fold_artifact_sha256": fold_reference["file_sha256"],
                        "held_out_asset_id": fold["held_out_asset_id"],
                    },
                ),
            )
            created += 0 if was_replayed else 1
            replayed += 1 if was_replayed else 0
        await complete_evaluation_run(
            session,
            evaluation_run_id=run.id,
            artifact_uri=str(fold_reference["artifact_uri"]),
            artifact_sha256=str(fold_reference["file_sha256"]),
            completed_at=completed_at,
            subject=subject,
        )
    return FullScaleEvaluationRegistration(
        created,
        replayed,
        tuple(model_ids),
        tuple(evaluation_run_ids),
    )
