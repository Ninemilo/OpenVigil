"""Full-scale evidence validation by rereading raw inputs and recomputing results."""

from __future__ import annotations

import importlib
import importlib.metadata
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import windops_backend.benchmarks.care.fullscale_import as _fullscale_import
from windops_backend.benchmarks.care.contract import CareContractError
from windops_backend.benchmarks.care.fullscale_evaluation_compute import (
    _accumulate_moments,
    _build_fold_profiles,
    _model_id,
    _quality_mask_impact_summary,
)
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _batch_status_evidence,
    _batch_values,
    _evaluation_column_contracts,
    _feature_matrix,
    _iter_event_batches,
    _load_json_reference,
    _load_quality_mask_artifact,
    _normalise_status,
    _quality_mask_index,
    _quality_mask_lineage,
    _quality_mask_matrix,
    _quality_mask_policy,
)
from windops_backend.benchmarks.care.pipeline import CareObjectStorageLayout
from windops_backend.benchmarks.care.quality import (
    STATUS_CORROBORATION_RULE_ID,
    StatusSequenceState,
    evaluate_status_point,
    status_evidence_policy,
)
from windops_backend.benchmarks.care.scoring import verify_prediction_artifact
from windops_backend.benchmarks.care.trust import CareTrustAnchor, verify_embedded_care_approval

FULL_SCALE_EVALUATION_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_EVALUATION_SCHEMA_VERSION
FULL_SCALE_FOLD_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_FOLD_SCHEMA_VERSION
FULL_SCALE_MODEL_SCHEMA_VERSION = _fullscale_import.FULL_SCALE_MODEL_SCHEMA_VERSION
FULL_SCALE_PREDICTION_FREEZE_SCHEMA_VERSION = (
    _fullscale_import.FULL_SCALE_PREDICTION_FREEZE_SCHEMA_VERSION
)
FULL_SCALE_OPERATIONAL_POLICY_VERSION = _fullscale_import.FULL_SCALE_OPERATIONAL_POLICY_VERSION
FULL_SCALE_THRESHOLD = _fullscale_import.FULL_SCALE_THRESHOLD
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
_canonical_hash = _fullscale_import._canonical_hash
_is_sha256 = _fullscale_import._is_sha256
_verify_license = _fullscale_import._verify_license


def verify_full_scale_prediction_status_evidence(
    prediction: Mapping[str, Any],
    *,
    output_root: Path,
    event: Mapping[str, Any],
    status_signal_columns: Sequence[str],
) -> dict[str, int]:
    """Recompute caller-supplied status fields from immutable Parquet rows and signals."""

    verify_prediction_artifact(prediction)
    farm = str(event["farm"])
    expected_policy = status_evidence_policy(farm, status_signal_columns)
    if prediction.get("status_evidence_policy") != expected_policy:
        raise CareFullScaleError("full-scale prediction status evidence policy is invalid")
    points = prediction.get("points")
    if not isinstance(points, list):
        raise CareFullScaleError("full-scale prediction points are missing")
    np = importlib.import_module("numpy")
    state = StatusSequenceState(farm, FULL_SCALE_TRAIN_STATUS_IDS)
    point_index = 0
    training_counts = {
        "status_usable_train_row_count": 0,
        "trusted_train_row_count": 0,
        "retained_disagreement_train_row_count": 0,
        "masked_status_train_row_count": 0,
    }
    columns = ("id", "train_test", "status_type_id", *status_signal_columns)
    for batch in _iter_event_batches(output_root, event, columns):
        row_ids = _batch_values(batch, "id")
        splits = _batch_values(batch, "train_test")
        statuses = _batch_values(batch, "status_type_id")
        signal_values = (
            _feature_matrix(batch, status_signal_columns, np)
            if status_signal_columns
            else np.empty((len(row_ids), 0), dtype=np.float64)
        )
        evidence = _batch_status_evidence(
            state=state,
            row_ids=row_ids,
            splits=splits,
            statuses=statuses,
            feature_values=signal_values,
            feature_columns=status_signal_columns,
            status_signal_columns=status_signal_columns,
        )
        for batch_index, split in enumerate(splits):
            status_id = _normalise_status(statuses[batch_index])
            if str(split) == "train":
                decision = evaluate_status_point(
                    farm=farm,
                    split="train",
                    status_id=status_id,
                    trusted_status_ids=FULL_SCALE_TRAIN_STATUS_IDS,
                    disagreement_run_length=evidence[batch_index].disagreement_run_length,
                    corroborated_by_signal=evidence[batch_index].corroborated_by_signal,
                )
                if decision.model_usable:
                    training_counts["status_usable_train_row_count"] += 1
                    if status_id in FULL_SCALE_TRAIN_STATUS_IDS:
                        training_counts["trusted_train_row_count"] += 1
                    else:
                        training_counts["retained_disagreement_train_row_count"] += 1
                else:
                    training_counts["masked_status_train_row_count"] += 1
            if str(split) != "prediction":
                continue
            if point_index >= len(points) or not isinstance(points[point_index], Mapping):
                raise CareFullScaleError("full-scale prediction status evidence is incomplete")
            point = points[point_index]
            expected = {
                "source_row_id": int(row_ids[batch_index]),
                "status_id": status_id,
                "disagreement_run_length": evidence[batch_index].disagreement_run_length,
                "corroborated_by_signal": evidence[batch_index].corroborated_by_signal,
                "corroboration_signal_count": evidence[batch_index].corroboration_signal_count,
                "corroboration_finite_signal_count": evidence[
                    batch_index
                ].corroboration_finite_signal_count,
                "corroboration_inactive_or_invalid_signal_count": evidence[
                    batch_index
                ].corroboration_inactive_or_invalid_signal_count,
                "corroboration_rule_id": STATUS_CORROBORATION_RULE_ID,
            }
            if any(point.get(key) != value for key, value in expected.items()):
                raise CareFullScaleError(
                    "full-scale prediction status evidence differs from raw status and signals"
                )
            point_index += 1
    if point_index != len(points):
        raise CareFullScaleError("full-scale prediction status evidence has extra points")
    return training_counts


def verify_full_scale_prediction_model_inputs(
    prediction: Mapping[str, Any],
    *,
    output_root: Path,
    event: Mapping[str, Any],
    features: Sequence[str],
    status_signal_columns: Sequence[str],
    profile: Mapping[str, Any],
) -> dict[str, int]:
    """Recompute quality-mask application and scores from immutable model inputs."""

    training_counts = verify_full_scale_prediction_status_evidence(
        prediction,
        output_root=output_root,
        event=event,
        status_signal_columns=status_signal_columns,
    )
    expected_policy = _quality_mask_policy(features)
    event_quality = event.get("quality")
    mask_reference = event_quality.get("mask") if isinstance(event_quality, Mapping) else None
    if (
        prediction.get("quality_mask_policy") != expected_policy
        or not isinstance(mask_reference, Mapping)
        or prediction.get("quality_mask_reference") != dict(mask_reference)
    ):
        raise CareFullScaleError("full-scale prediction quality mask lineage is invalid")
    mask_artifact = _load_quality_mask_artifact(output_root, event)
    mask_index = _quality_mask_index(mask_artifact, features)
    selected_columns = set(features)
    selected_range_count = sum(
        1
        for item in mask_artifact["quality_mask_ranges"]
        if str(item["source_column"]) in selected_columns
    )
    if (
        prediction.get("quality_mask_range_count") != mask_artifact.get("mask_count")
        or prediction.get("selected_quality_mask_range_count") != selected_range_count
    ):
        raise CareFullScaleError("full-scale prediction quality mask counts are invalid")

    points = prediction.get("points")
    if not isinstance(points, list):
        raise CareFullScaleError("full-scale prediction points are missing")
    np = importlib.import_module("numpy")
    means = np.asarray(profile["means"], dtype=np.float64)
    standard_deviations = np.asarray(profile["standard_deviations"], dtype=np.float64)
    point_index = 0
    masked_value_count = 0
    masked_point_count = 0
    nonfinite_value_count = 0
    imputed_value_count = 0
    score_changed_count = 0
    binary_changed_count = 0
    absolute_score_deltas: list[float] = []
    columns = ("id", "train_test", *features)
    for batch in _iter_event_batches(output_root, event, columns):
        row_ids = _batch_values(batch, "id")
        splits = _batch_values(batch, "train_test")
        all_values = _feature_matrix(batch, features, np)
        all_masks = _quality_mask_matrix(
            row_ids=row_ids,
            feature_columns=features,
            mask_index=mask_index,
            np=np,
        )
        prediction_positions = [
            index for index, value in enumerate(splits) if str(value) == "prediction"
        ]
        if not prediction_positions:
            continue
        values = all_values[prediction_positions]
        quality_masks = all_masks[prediction_positions]
        finite = np.isfinite(values)
        usable = finite & ~quality_masks
        imputed = np.where(usable, values, means)
        scores = np.max(np.abs((imputed - means) / standard_deviations), axis=1)
        ignored_mask_values = np.where(finite, values, means)
        ignored_mask_scores = np.max(
            np.abs((ignored_mask_values - means) / standard_deviations),
            axis=1,
        )
        for score_index, batch_index in enumerate(prediction_positions):
            if point_index >= len(points) or not isinstance(points[point_index], Mapping):
                raise CareFullScaleError("full-scale prediction model-input evidence is incomplete")
            point = points[point_index]
            quality_count = int(quality_masks[score_index].sum())
            nonfinite_count = int((~finite[score_index]).sum())
            imputed_count = int((~usable[score_index]).sum())
            score = float(scores[score_index])
            ignored_score = float(ignored_mask_scores[score_index])
            score_delta = score - ignored_score
            binary_changed = (score > FULL_SCALE_THRESHOLD) != (
                ignored_score > FULL_SCALE_THRESHOLD
            )
            if (
                point.get("source_row_id") != int(row_ids[batch_index])
                or point.get("quality_masked_feature_count") != quality_count
                or point.get("nonfinite_feature_count") != nonfinite_count
                or point.get("imputed_feature_count") != imputed_count
                or point.get("anomaly_score") != score
                or point.get("quality_mask_ignored_anomaly_score") != ignored_score
                or point.get("quality_mask_score_delta") != score_delta
                or point.get("quality_mask_changed_binary_prediction") is not binary_changed
            ):
                raise CareFullScaleError(
                    "full-scale prediction differs from raw values, quality mask, and fold"
                )
            masked_value_count += quality_count
            masked_point_count += int(quality_count > 0)
            nonfinite_value_count += nonfinite_count
            imputed_value_count += imputed_count
            score_changed_count += int(score_delta != 0.0)
            binary_changed_count += int(binary_changed)
            absolute_score_deltas.append(abs(score_delta))
            point_index += 1
    if point_index != len(points):
        raise CareFullScaleError("full-scale prediction model-input evidence has extra points")
    expected_counts = {
        "quality_masked_prediction_feature_value_count": masked_value_count,
        "quality_masked_prediction_point_count": masked_point_count,
        "nonfinite_prediction_feature_value_count": nonfinite_value_count,
        "imputed_prediction_feature_value_count": imputed_value_count,
        "quality_mask_score_changed_prediction_point_count": score_changed_count,
        "quality_mask_binary_decision_changed_point_count": binary_changed_count,
        "quality_mask_max_absolute_score_delta": max(absolute_score_deltas, default=0.0),
        "quality_mask_mean_absolute_score_delta": (
            sum(absolute_score_deltas) / len(absolute_score_deltas)
        ),
    }
    if any(prediction.get(name) != count for name, count in expected_counts.items()):
        raise CareFullScaleError("full-scale prediction model-input totals are invalid")
    if prediction.get("quality_mask_impact_interpretation") != (
        "counterfactual ignores prediction-time masks while retaining the same "
        "mask-trained fold; training-profile changes are proven separately by "
        "raw fold recomputation"
    ):
        raise CareFullScaleError("full-scale prediction mask impact interpretation is invalid")
    return training_counts


def _operational_policy(resource_limits: FullScaleResourceLimits) -> dict[str, Any]:
    payload = {
        "policy_version": FULL_SCALE_OPERATIONAL_POLICY_VERSION,
        "permissions": {
            "catalog": ["benchmark"],
            "truth": ["benchmark", "benchmark_truth"],
            "export": ["benchmark", "model", "benchmark_export"],
            "external_export_review": "required",
            "normal_worker_delete_allowed": False,
            "raw_cleanup_allowed": False,
        },
        "backup_recovery": {
            "bucket": CareObjectStorageLayout().bucket,
            "required_layers": ["raw", "standard", "quality", "predictions", "reports"],
            "versioning_required": True,
            "restore_requires_hash_verification": True,
            "restore_requires_sha256_object_metadata": True,
            "care_authoritative_table_invariants_required": True,
            "temporary_prefix_backed_up": False,
            "database_contains_full_signal_expansion": False,
        },
        "fault_recovery": {
            "storage_unavailable_state": "pending-or-failed-observable",
            "corrupt_checkpoint_state": "failed-closed",
            "cancel_state": "cancelled",
            "resume_mechanism": "new-job-id-reuses-verified-event-checkpoints",
            "maximum_attempts_per_job": 3,
            "partial_final_manifest_allowed": False,
        },
        "serving_limits": {
            "catalog_event_page_rows": resource_limits.catalog_event_page_rows,
            "curve_response_points": resource_limits.curve_response_points,
            "frontend_api_response_bytes": resource_limits.frontend_api_response_bytes,
            "query_p95_milliseconds": resource_limits.query_p95_milliseconds,
            "replay_batch_rows": resource_limits.replay_batch_rows,
            "min_replay_write_rows_per_second": MIN_REPLAY_WRITE_ROWS_PER_SECOND,
        },
    }
    return {**payload, "policy_sha256": _canonical_hash(payload)}


def _verify_operational_policy(value: Mapping[str, Any]) -> None:
    actual = value.get("policy_sha256")
    unsigned = {key: item for key, item in value.items() if key != "policy_sha256"}
    permissions = value.get("permissions")
    backup = value.get("backup_recovery")
    fault = value.get("fault_recovery")
    serving = value.get("serving_limits")
    if (
        not _is_sha256(actual)
        or _canonical_hash(unsigned) != actual
        or value.get("policy_version") != FULL_SCALE_OPERATIONAL_POLICY_VERSION
        or not isinstance(permissions, Mapping)
        or permissions.get("normal_worker_delete_allowed") is not False
        or permissions.get("raw_cleanup_allowed") is not False
        or not isinstance(backup, Mapping)
        or backup.get("required_layers") != ["raw", "standard", "quality", "predictions", "reports"]
        or backup.get("restore_requires_sha256_object_metadata") is not True
        or backup.get("care_authoritative_table_invariants_required") is not True
        or backup.get("temporary_prefix_backed_up") is not False
        or backup.get("database_contains_full_signal_expansion") is not False
        or not isinstance(fault, Mapping)
        or fault.get("partial_final_manifest_allowed") is not False
        or not isinstance(serving, Mapping)
        or serving.get("min_replay_write_rows_per_second") != MIN_REPLAY_WRITE_ROWS_PER_SECOND
    ):
        raise CareFullScaleError("full-scale operational policy is invalid")


def _verify_model_package(value: Mapping[str, Any]) -> None:
    actual = value.get("model_package_sha256")
    unsigned = {key: item for key, item in value.items() if key != "model_package_sha256"}
    profiles = value.get("fold_profiles")
    features = value.get("feature_columns")
    farm = value.get("farm")
    status_policy = value.get("status_evidence_policy")
    quality_mask_policy = value.get("quality_mask_policy")
    quality_mask_lineage = value.get("quality_mask_lineage")
    if (
        not _is_sha256(actual)
        or _canonical_hash(unsigned) != actual
        or value.get("schema_version") != FULL_SCALE_MODEL_SCHEMA_VERSION
        or value.get("model_kind") != "anomaly"
        or value.get("model_version") != FULL_SCALE_MODEL_VERSION
        or value.get("generalization_protocol") != "within-farm-leave-one-turbine-out-v1"
        or value.get("prediction_truth_used") is not False
        or not isinstance(features, list)
        or not features
        or not isinstance(profiles, list)
        or not profiles
        or farm not in FULL_SCALE_STAGE_ORDER
        or not isinstance(status_policy, Mapping)
        or not isinstance(quality_mask_policy, Mapping)
        or not isinstance(quality_mask_lineage, Mapping)
    ):
        raise CareFullScaleError("full-scale model package identity is invalid")
    expected_quality_mask_policy = _quality_mask_policy([str(item) for item in features])
    if dict(quality_mask_policy) != expected_quality_mask_policy:
        raise CareFullScaleError("full-scale model quality mask policy drifted")
    lineage_hash = quality_mask_lineage.get("lineage_sha256")
    lineage_unsigned = {
        key: item for key, item in quality_mask_lineage.items() if key != "lineage_sha256"
    }
    lineage_events = quality_mask_lineage.get("events")
    if (
        not _is_sha256(lineage_hash)
        or _canonical_hash(lineage_unsigned) != lineage_hash
        or quality_mask_lineage.get("farm") != farm
        or quality_mask_lineage.get("excluded_asset_id") is not None
        or not isinstance(lineage_events, list)
        or quality_mask_lineage.get("event_count") != len(lineage_events)
        or not lineage_events
    ):
        raise CareFullScaleError("full-scale model quality mask lineage is invalid")
    seen_mask_events: set[int] = set()
    for lineage_event in lineage_events:
        if not isinstance(lineage_event, Mapping):
            raise CareFullScaleError("full-scale model quality mask event is malformed")
        event_id = lineage_event.get("event_id")
        mask_count = lineage_event.get("mask_range_count")
        if (
            not isinstance(event_id, int)
            or isinstance(event_id, bool)
            or event_id in seen_mask_events
            or not isinstance(lineage_event.get("source_asset_id"), str)
            or not _is_sha256(lineage_event.get("quality_mask_file_sha256"))
            or not _is_sha256(lineage_event.get("source_quality_report_sha256"))
            or not isinstance(mask_count, int)
            or isinstance(mask_count, bool)
            or mask_count < 0
        ):
            raise CareFullScaleError("full-scale model quality mask event is invalid")
        seen_mask_events.add(event_id)
    status_signal_columns = status_policy.get("corroboration_signal_columns")
    if (
        not isinstance(status_signal_columns, list)
        or any(not isinstance(item, str) for item in status_signal_columns)
        or any(item not in features for item in status_signal_columns)
    ):
        raise CareFullScaleError("full-scale model status evidence policy is malformed")
    try:
        expected_status_policy = status_evidence_policy(str(farm), status_signal_columns)
    except CareContractError as exc:
        raise CareFullScaleError(str(exc)) from exc
    if dict(status_policy) != expected_status_policy:
        raise CareFullScaleError("full-scale model status evidence policy drifted")
    expected_length = len(features)
    held_out: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, Mapping):
            raise CareFullScaleError("full-scale model fold profile is malformed")
        asset = str(profile.get("held_out_asset_id", ""))
        if not asset or asset in held_out:
            raise CareFullScaleError("full-scale model fold identity is duplicated")
        held_out.add(asset)
        usable_rows = profile.get("status_usable_train_row_count")
        trusted_rows = profile.get("trusted_train_row_count")
        retained_rows = profile.get("retained_disagreement_train_row_count")
        masked_rows = profile.get("masked_status_train_row_count")
        quality_masked_rows = profile.get("quality_masked_train_row_count")
        quality_masked_values = profile.get("quality_masked_train_feature_value_count")
        quality_mask_counts = profile.get("quality_masked_train_feature_counts")
        quality_mask_event_count = profile.get("training_quality_mask_event_count")
        if (
            not isinstance(usable_rows, int)
            or isinstance(usable_rows, bool)
            or usable_rows < 0
            or not isinstance(trusted_rows, int)
            or isinstance(trusted_rows, bool)
            or trusted_rows < 0
            or not isinstance(retained_rows, int)
            or isinstance(retained_rows, bool)
            or retained_rows < 0
            or not isinstance(masked_rows, int)
            or isinstance(masked_rows, bool)
            or masked_rows < 0
            or not isinstance(quality_masked_rows, int)
            or isinstance(quality_masked_rows, bool)
            or quality_masked_rows < 0
            or quality_masked_rows > usable_rows
            or not isinstance(quality_masked_values, int)
            or isinstance(quality_masked_values, bool)
            or quality_masked_values < 0
            or not isinstance(quality_mask_counts, list)
            or len(quality_mask_counts) != expected_length
            or any(
                not isinstance(item, int) or isinstance(item, bool) or item < 0
                for item in quality_mask_counts
            )
            or sum(quality_mask_counts) != quality_masked_values
            or profile.get("quality_mask_policy_sha256") != quality_mask_policy["policy_sha256"]
            or not _is_sha256(profile.get("training_quality_mask_lineage_sha256"))
            or not isinstance(quality_mask_event_count, int)
            or isinstance(quality_mask_event_count, bool)
            or quality_mask_event_count < 1
            or profile.get("imputation")
            != "leave-one-asset-out-feature-mean-for-nonfinite-or-quality-masked-v2"
            or usable_rows != trusted_rows + retained_rows
        ):
            raise CareFullScaleError(f"full-scale fold {asset} status counts are invalid")
        for name in ("finite_counts", "means", "standard_deviations"):
            values = profile.get(name)
            if not isinstance(values, list) or len(values) != expected_length:
                raise CareFullScaleError(f"full-scale fold {asset} {name} shape is invalid")
            if any(
                isinstance(item, bool)
                or not isinstance(item, int | float)
                or not math.isfinite(float(item))
                for item in values
            ):
                raise CareFullScaleError(f"full-scale fold {asset} {name} is non-finite")
    _verify_license(value)


def _evaluate_resource_gate(
    *,
    elapsed_seconds: float,
    storage_bytes: int,
    prediction_count: int,
    peak_batch: int,
    peak_arrow: int,
    peak_resident: int,
    limits: FullScaleResourceLimits,
) -> dict[str, Any]:
    violations = []
    if elapsed_seconds > limits.max_evaluation_runtime_seconds:
        violations.append("max_evaluation_runtime_seconds")
    if storage_bytes > limits.max_evaluation_storage_bytes:
        violations.append("max_evaluation_storage_bytes")
    if prediction_count > limits.max_prediction_points:
        violations.append("max_prediction_points")
    if peak_batch > limits.max_peak_record_batch_bytes:
        violations.append("max_peak_record_batch_bytes")
    if peak_arrow > limits.max_peak_arrow_allocated_bytes:
        violations.append("max_peak_arrow_allocated_bytes")
    if peak_resident > limits.max_peak_process_resident_bytes:
        violations.append("max_peak_process_resident_bytes")
    if violations:
        raise CareFullScaleResourceError(
            f"full-scale evaluation exceeded preregistered resources: {', '.join(violations)}"
        )
    return {
        "elapsed_seconds": elapsed_seconds,
        "storage_bytes": storage_bytes,
        "prediction_point_count": prediction_count,
        "peak_record_batch_bytes": peak_batch,
        "peak_arrow_allocated_bytes": peak_arrow,
        "peak_process_resident_bytes": peak_resident,
        "limits_passed": True,
        "violations": [],
    }


def verify_full_scale_evaluation_manifest(
    value: Mapping[str, Any],
    *,
    output_root: Path,
    import_manifest: Mapping[str, Any],
    expected_counts: FullScaleExpectedCounts = CARE_V6_FULL_SCALE_COUNTS,
    resource_limits: FullScaleResourceLimits = DEFAULT_FULL_SCALE_RESOURCE_LIMITS,
    trust_anchor: CareTrustAnchor | None = None,
) -> None:
    actual = value.get("manifest_sha256")
    unsigned = {key: item for key, item in value.items() if key != "manifest_sha256"}
    summary = value.get("summary")
    model_packages = value.get("model_packages")
    prediction_freeze = value.get("prediction_freeze")
    fold_artifacts = value.get("fold_artifacts")
    fold_summaries = value.get("fold_summaries")
    if (
        not _is_sha256(actual)
        or _canonical_hash(unsigned) != actual
        or value.get("schema_version") != FULL_SCALE_EVALUATION_SCHEMA_VERSION
        or value.get("source_import_manifest_sha256") != import_manifest["manifest_sha256"]
        or value.get("generalization_protocol") != "within-farm-leave-one-turbine-out-v1"
        or value.get("farm_namespaced_fold_count") != expected_counts.asset_count
        or not isinstance(summary, Mapping)
        or not isinstance(model_packages, Mapping)
        or not isinstance(prediction_freeze, Mapping)
        or not isinstance(fold_artifacts, list)
        or not isinstance(fold_summaries, list)
        or len(fold_artifacts) != expected_counts.asset_count
        or len(fold_summaries) != expected_counts.asset_count
    ):
        raise CareFullScaleError("full-scale evaluation manifest identity is invalid")
    approval = value.get("care_approval")
    if not isinstance(approval, Mapping) or approval != import_manifest.get("care_approval"):
        raise CareFullScaleError("full-scale evaluation approved-root lineage is invalid")
    try:
        verify_embedded_care_approval(approval, trust_anchor=trust_anchor)
    except CareContractError as exc:
        raise CareFullScaleError(str(exc)) from exc
    for key, expected in expected_counts.to_document().items():
        if summary.get(key) != expected:
            raise CareFullScaleError(f"full-scale evaluation summary {key} is invalid")
    if (
        summary.get("prediction_event_count") != expected_counts.event_count
        or summary.get("fold_count") != expected_counts.asset_count
        or summary.get("all_events_accounted_for") is not True
    ):
        raise CareFullScaleError("full-scale evaluation outcome coverage is incomplete")
    if (
        expected_counts == CARE_V6_FULL_SCALE_COUNTS
        and summary.get("prediction_point_count") != resource_prediction_count()
    ):
        raise CareFullScaleError("CARE v6 prediction point count is inconsistent")
    features_by_farm, status_signals_by_farm = _evaluation_column_contracts(
        output_root,
        import_manifest,
    )
    loaded_model_packages: dict[str, Mapping[str, Any]] = {}
    for farm, reference in model_packages.items():
        if farm not in FULL_SCALE_STAGE_ORDER or not isinstance(reference, Mapping):
            raise CareFullScaleError("full-scale model reference is malformed")
        model_package = _load_json_reference(output_root, reference)
        _verify_model_package(model_package)
        if (
            model_package.get("feature_columns") != list(features_by_farm[farm])
            or model_package.get("status_evidence_policy")
            != status_evidence_policy(farm, status_signals_by_farm[farm])
            or model_package.get("quality_mask_policy")
            != _quality_mask_policy(features_by_farm[farm])
            or model_package.get("quality_mask_lineage")
            != _quality_mask_lineage(import_manifest, farm)
        ):
            raise CareFullScaleError(
                "full-scale model inputs differ from the approved import mapping or masks"
            )
        loaded_model_packages[farm] = model_package
    if set(model_packages) != set(FULL_SCALE_STAGE_ORDER):
        raise CareFullScaleError("full-scale model packages do not cover every farm")
    freeze = _load_json_reference(output_root, prediction_freeze)
    freeze_unsigned = {key: item for key, item in freeze.items() if key != "freeze_sha256"}
    if (
        freeze.get("schema_version") != FULL_SCALE_PREDICTION_FREEZE_SCHEMA_VERSION
        or _canonical_hash(freeze_unsigned) != freeze.get("freeze_sha256")
        or freeze.get("prediction_truth_present") is not False
        or freeze.get("prediction_truth_read") is not False
        or freeze.get("quality_mask_policy_version") != FULL_SCALE_QUALITY_MASK_POLICY_VERSION
        or freeze.get("quality_masks_applied") is not True
        or freeze.get("prediction_event_count") != expected_counts.event_count
    ):
        raise CareFullScaleError("full-scale prediction freeze is invalid")
    prediction_references = freeze.get("prediction_artifacts")
    training_resources = value.get("training_resources")
    prediction_resources = value.get("prediction_resources")
    resource_actual = value.get("resource_actual")
    if (
        not isinstance(prediction_references, Mapping)
        or not isinstance(training_resources, Mapping)
        or not isinstance(prediction_resources, list)
        or len(prediction_resources) != expected_counts.event_count
        or any(not isinstance(item, Mapping) for item in prediction_resources)
        or any(not isinstance(item, Mapping) for item in fold_artifacts)
        or not isinstance(resource_actual, Mapping)
    ):
        raise CareFullScaleError("full-scale evaluation resource evidence is malformed")
    verification_totals, verification_assets, verification_training = _accumulate_moments(
        import_manifest=import_manifest,
        output_root=output_root,
        features_by_farm=features_by_farm,
        status_signals_by_farm=status_signals_by_farm,
    )
    expected_profiles = _build_fold_profiles(
        totals=verification_totals,
        assets=verification_assets,
        features_by_farm=features_by_farm,
        import_manifest=import_manifest,
    )
    verified_training_count_fields = (
        "scanned_row_count",
        "status_usable_train_row_count",
        "trusted_train_row_count",
        "retained_disagreement_train_row_count",
        "masked_status_train_row_count",
        "quality_masked_train_row_count",
        "quality_masked_train_feature_value_count",
    )
    if any(
        training_resources.get(name) != verification_training.get(name)
        for name in verified_training_count_fields
    ):
        raise CareFullScaleError(
            "full-scale training totals differ from raw rows and quality masks"
        )
    for farm, model_package in loaded_model_packages.items():
        for profile in model_package["fold_profiles"]:
            profile_key = (farm, str(profile["held_out_asset_id"]))
            if (
                profile_key not in expected_profiles
                or dict(profile) != expected_profiles[profile_key]
            ):
                raise CareFullScaleError(
                    "full-scale fold moments differ from raw rows and quality masks"
                )
    expected_events = {str(int(event["event_id"])): event for event in import_manifest["events"]}
    if set(prediction_references) != set(expected_events):
        raise CareFullScaleError("full-scale prediction references are incomplete")
    prediction_point_count = 0
    verified_predictions: list[Mapping[str, Any]] = []
    verified_prediction_by_event: dict[int, Mapping[str, Any]] = {}
    raw_training_counts_by_asset: dict[tuple[str, str], dict[str, int]] = {}
    for event_id, event in expected_events.items():
        reference = prediction_references[event_id]
        if not isinstance(reference, Mapping):
            raise CareFullScaleError("full-scale prediction reference is malformed")
        prediction = _load_json_reference(output_root, reference)
        verified_predictions.append(prediction)
        verified_prediction_by_event[int(event_id)] = prediction
        profile = expected_profiles[(str(event["farm"]), str(event["source_asset_id"]))]
        event_training_counts = verify_full_scale_prediction_model_inputs(
            prediction,
            output_root=output_root,
            event=event,
            features=features_by_farm[str(event["farm"])],
            status_signal_columns=status_signals_by_farm[str(event["farm"])],
            profile=profile,
        )
        asset_key = (str(event["farm"]), str(event["source_asset_id"]))
        asset_counts = raw_training_counts_by_asset.setdefault(
            asset_key,
            {name: 0 for name in event_training_counts},
        )
        for name, count in event_training_counts.items():
            asset_counts[name] += count
        if (
            prediction.get("event_id") != int(event_id)
            or prediction.get("farm") != event["farm"]
            or str(prediction.get("source_asset_id")) != str(event["source_asset_id"])
            or prediction.get("model_id") != _model_id(str(event["farm"]))
            or prediction.get("point_count") != int(event["split_counts"]["prediction"])
        ):
            raise CareFullScaleError("full-scale prediction identity is invalid")
        prediction_point_count += int(prediction["point_count"])
    if (
        prediction_point_count != int(summary["prediction_point_count"])
        or sum(int(item["prediction_point_count"]) for item in prediction_resources)
        != prediction_point_count
    ):
        raise CareFullScaleError("full-scale prediction resource count is invalid")
    if value.get("quality_mask_impact") != _quality_mask_impact_summary(
        training_resources,
        verified_predictions,
    ):
        raise CareFullScaleError("full-scale quality mask impact summary is invalid")
    raw_training_totals = {
        name: sum(counts[name] for counts in raw_training_counts_by_asset.values())
        for name in (
            "status_usable_train_row_count",
            "trusted_train_row_count",
            "retained_disagreement_train_row_count",
            "masked_status_train_row_count",
        )
    }
    if any(training_resources.get(name) != count for name, count in raw_training_totals.items()):
        raise CareFullScaleError("full-scale training status totals differ from raw rows")
    for farm, model_package in loaded_model_packages.items():
        for profile in model_package["fold_profiles"]:
            held_out_asset = str(profile["held_out_asset_id"])
            expected_profile_counts = {
                name: sum(
                    counts[name]
                    for (item_farm, item_asset), counts in raw_training_counts_by_asset.items()
                    if item_farm == farm and item_asset != held_out_asset
                )
                for name in raw_training_totals
            }
            if any(profile.get(name) != count for name, count in expected_profile_counts.items()):
                raise CareFullScaleError(
                    "full-scale fold status counts differ from raw status and signals"
                )
    actual_elapsed = resource_actual.get("elapsed_seconds")
    actual_peak_resident = resource_actual.get("peak_process_resident_bytes")
    if (
        not isinstance(actual_elapsed, int | float)
        or isinstance(actual_elapsed, bool)
        or not math.isfinite(float(actual_elapsed))
        or float(actual_elapsed) <= 0
        or not isinstance(actual_peak_resident, int)
        or isinstance(actual_peak_resident, bool)
        or actual_peak_resident <= 0
    ):
        raise CareFullScaleError("full-scale evaluation resource actual is malformed")
    expected_resource_actual = _evaluate_resource_gate(
        elapsed_seconds=float(actual_elapsed),
        storage_bytes=sum(
            int(reference["size_bytes"])
            for reference in [
                *model_packages.values(),
                *prediction_references.values(),
                prediction_freeze,
                *fold_artifacts,
            ]
        ),
        prediction_count=prediction_point_count,
        peak_batch=max(
            int(training_resources["peak_record_batch_bytes"]),
            max(
                (int(item["peak_record_batch_bytes"]) for item in prediction_resources),
                default=0,
            ),
        ),
        peak_arrow=max(
            int(training_resources["peak_arrow_allocated_bytes"]),
            max(
                (int(item["peak_arrow_allocated_bytes"]) for item in prediction_resources),
                default=0,
            ),
        ),
        peak_resident=actual_peak_resident,
        limits=resource_limits,
    )
    if dict(resource_actual) != expected_resource_actual:
        raise CareFullScaleError("full-scale evaluation resource actual is invalid")
    accounted: set[int] = set()
    freeze_hash = freeze["freeze_sha256"]
    for reference, summary_item in zip(fold_artifacts, fold_summaries, strict=True):
        if not isinstance(reference, Mapping) or not isinstance(summary_item, Mapping):
            raise CareFullScaleError("full-scale fold reference is malformed")
        fold = _load_json_reference(output_root, reference)
        fold_hash = fold.get("document_sha256")
        fold_unsigned = {key: item for key, item in fold.items() if key != "document_sha256"}
        evaluation = fold.get("evaluation")
        farm = str(fold.get("farm", ""))
        held_out_asset = str(fold.get("held_out_asset_id", ""))
        if (
            fold.get("schema_version") != FULL_SCALE_FOLD_SCHEMA_VERSION
            or _canonical_hash(fold_unsigned) != fold_hash
            or fold.get("prediction_freeze_sha256") != freeze_hash
            or fold.get("truth_read_after_prediction_freeze") is not True
            or not isinstance(evaluation, Mapping)
            or evaluation.get("run", {}).get("generalization_protocol")
            != "within-farm-leave-one-turbine-out-v1"
            or farm not in loaded_model_packages
            or fold.get("model_package_sha256")
            != loaded_model_packages[farm]["model_package_sha256"]
            or fold.get("quality_mask_policy") != _quality_mask_policy(features_by_farm[farm])
            or fold.get("fold_profile") != expected_profiles.get((farm, held_out_asset))
        ):
            raise CareFullScaleError("full-scale fold artifact identity is invalid")
        event_ids = {int(item) for item in summary_item.get("event_ids", [])}
        if not event_ids.issubset(verified_prediction_by_event):
            raise CareFullScaleError("full-scale fold prediction mask coverage is incomplete")
        expected_prediction_quality_masks = [
            {
                "event_id": event_id,
                "quality_mask_file_sha256": verified_prediction_by_event[event_id][
                    "quality_mask_reference"
                ]["file_sha256"],
                "quality_mask_range_count": verified_prediction_by_event[event_id][
                    "quality_mask_range_count"
                ],
                "quality_masked_prediction_feature_value_count": verified_prediction_by_event[
                    event_id
                ]["quality_masked_prediction_feature_value_count"],
                "quality_masked_prediction_point_count": verified_prediction_by_event[event_id][
                    "quality_masked_prediction_point_count"
                ],
            }
            for event_id in sorted(event_ids)
        ]
        if fold.get("prediction_quality_masks") != expected_prediction_quality_masks:
            raise CareFullScaleError("full-scale fold prediction mask lineage is invalid")
        if accounted.intersection(event_ids):
            raise CareFullScaleError("full-scale fold event coverage overlaps")
        accounted.update(event_ids)
        _verify_license(fold)
    expected_event_ids = {int(event["event_id"]) for event in import_manifest["events"]}
    if accounted != expected_event_ids:
        raise CareFullScaleError("full-scale fold artifacts do not cover every event")
    cross_farm = value.get("cross_farm_protocol")
    release_gate = value.get("server_release_gate")
    truth_boundary = value.get("truth_boundary")
    operational = value.get("operational_policy")
    if (
        not isinstance(cross_farm, Mapping)
        or cross_farm.get("status") != "disabled"
        or cross_farm.get("human_review_ids") != []
        or not isinstance(release_gate, Mapping)
        or release_gate.get("frontend_or_json_override_allowed") is not False
        or value.get("resource_policy") != resource_limits.to_document()
        or not isinstance(truth_boundary, Mapping)
        or truth_boundary.get("prediction_freeze_completed_before_truth_read") is not True
        or truth_boundary.get("prediction_truth_available_to_training_or_calibration") is not False
        or not isinstance(operational, Mapping)
        or dict(operational) != _operational_policy(resource_limits)
    ):
        raise CareFullScaleError("full-scale release, resource, truth, or ontology gate is invalid")
    _verify_operational_policy(operational)
    _verify_license(value)


def resource_prediction_count() -> int:
    """Frozen CARE v6 prediction split count used by release verification."""

    return 281_249
