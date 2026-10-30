"""Full-scale training and prediction computation over bounded Parquet batches."""

from __future__ import annotations

import importlib
import importlib.metadata
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import windops_backend.benchmarks.care.fullscale_import as _fullscale_import
from windops_backend.benchmarks.care.fullscale_evaluation_inputs import (
    _batch_status_evidence,
    _batch_values,
    _feature_matrix,
    _iter_event_batches,
    _load_quality_mask_artifact,
    _normalise_status,
    _quality_mask_index,
    _quality_mask_lineage,
    _quality_mask_matrix,
    _quality_mask_policy,
    _timestamp_text,
)
from windops_backend.benchmarks.care.pipeline import FEATURE_SET_VERSION, QUALITY_RULE_VERSION
from windops_backend.benchmarks.care.quality import StatusSequenceState, evaluate_status_point
from windops_backend.benchmarks.care.resources import trim_process_resident_memory
from windops_backend.benchmarks.care.scoring import (
    ModelSelectionProvenance,
    PredictionBatch,
    PredictionPoint,
    ThresholdPolicy,
    build_prediction_artifact,
    verify_prediction_artifact,
)

FULL_SCALE_THRESHOLD = _fullscale_import.FULL_SCALE_THRESHOLD
FULL_SCALE_THRESHOLD_VERSION = _fullscale_import.FULL_SCALE_THRESHOLD_VERSION
FULL_SCALE_THRESHOLD_RUN_ID = _fullscale_import.FULL_SCALE_THRESHOLD_RUN_ID
FULL_SCALE_MODEL_VERSION = _fullscale_import.FULL_SCALE_MODEL_VERSION
FULL_SCALE_QUALITY_MASK_POLICY_VERSION = _fullscale_import.FULL_SCALE_QUALITY_MASK_POLICY_VERSION
FULL_SCALE_TRAIN_STATUS_IDS = _fullscale_import.FULL_SCALE_TRAIN_STATUS_IDS
CareFullScaleError = _fullscale_import.CareFullScaleError
_canonical_hash = _fullscale_import._canonical_hash
_safe_token = _fullscale_import._safe_token


@dataclass(slots=True)
class _Moments:
    counts: Any
    sums: Any
    sum_squares: Any
    quality_mask_counts: Any
    status_usable_train_rows: int = 0
    trusted_train_rows: int = 0
    retained_disagreement_train_rows: int = 0
    masked_status_train_rows: int = 0
    quality_masked_train_rows: int = 0


def _new_moments(np: Any, feature_count: int) -> _Moments:
    return _Moments(
        counts=np.zeros(feature_count, dtype=np.int64),
        sums=np.zeros(feature_count, dtype=np.float64),
        sum_squares=np.zeros(feature_count, dtype=np.float64),
        quality_mask_counts=np.zeros(feature_count, dtype=np.int64),
    )


def _quality_mask_impact_summary(
    training_resources: Mapping[str, Any],
    predictions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    prediction_point_count = sum(int(item["point_count"]) for item in predictions)
    absolute_delta_sum = sum(
        float(item["quality_mask_mean_absolute_score_delta"]) * int(item["point_count"])
        for item in predictions
    )
    payload = {
        "policy_version": FULL_SCALE_QUALITY_MASK_POLICY_VERSION,
        "decision": "apply",
        "quality_masked_train_row_count": int(training_resources["quality_masked_train_row_count"]),
        "quality_masked_train_feature_value_count": int(
            training_resources["quality_masked_train_feature_value_count"]
        ),
        "quality_masked_prediction_point_count": sum(
            int(item["quality_masked_prediction_point_count"]) for item in predictions
        ),
        "quality_masked_prediction_feature_value_count": sum(
            int(item["quality_masked_prediction_feature_value_count"]) for item in predictions
        ),
        "quality_mask_score_changed_prediction_point_count": sum(
            int(item["quality_mask_score_changed_prediction_point_count"]) for item in predictions
        ),
        "quality_mask_binary_decision_changed_point_count": sum(
            int(item["quality_mask_binary_decision_changed_point_count"]) for item in predictions
        ),
        "quality_mask_max_absolute_score_delta": max(
            (float(item["quality_mask_max_absolute_score_delta"]) for item in predictions),
            default=0.0,
        ),
        "quality_mask_mean_absolute_score_delta": (
            absolute_delta_sum / prediction_point_count if prediction_point_count else 0.0
        ),
        "actual_fold_metrics_use_masked_predictions": True,
        "counterfactual_scope": (
            "prediction-time masks ignored with the same mask-trained fold profile"
        ),
        "metric_change_explanation": (
            "fold metrics are recomputed from mask-applied scores; score and binary-decision "
            "delta counts quantify prediction-time impact, while verifier-recomputed fold "
            "moments prove training-time impact"
        ),
    }
    return {**payload, "impact_sha256": _canonical_hash(payload)}


def _accumulate_moments(
    *,
    import_manifest: Mapping[str, Any],
    output_root: Path,
    features_by_farm: Mapping[str, tuple[str, ...]],
    status_signals_by_farm: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, _Moments], dict[tuple[str, str], _Moments], dict[str, Any]]:
    np = importlib.import_module("numpy")
    arrow = importlib.import_module("pyarrow")
    totals = {farm: _new_moments(np, len(features)) for farm, features in features_by_farm.items()}
    assets: dict[tuple[str, str], _Moments] = {}
    baseline_arrow = int(arrow.total_allocated_bytes())
    peak_batch = 0
    peak_arrow = 0
    scanned_rows = 0
    status_usable_rows = 0
    trusted_rows = 0
    retained_disagreement_rows = 0
    masked_status_rows = 0
    quality_masked_rows = 0
    quality_masked_values = 0
    started = time.perf_counter()
    for event in import_manifest["events"]:
        farm = str(event["farm"])
        asset = str(event["source_asset_id"])
        features = features_by_farm[farm]
        status_signals = status_signals_by_farm[farm]
        mask_artifact = _load_quality_mask_artifact(output_root, event)
        mask_index = _quality_mask_index(mask_artifact, features)
        asset_moments = assets.setdefault((farm, asset), _new_moments(np, len(features)))
        status_state = StatusSequenceState(farm, FULL_SCALE_TRAIN_STATUS_IDS)
        columns = ("asset_id", "id", "train_test", "status_type_id", *features)
        for batch in _iter_event_batches(output_root, event, columns):
            peak_batch = max(peak_batch, int(batch.nbytes))
            peak_arrow = max(
                peak_arrow,
                max(0, int(arrow.total_allocated_bytes()) - baseline_arrow),
            )
            batch_assets = {_normalise_status(value) for value in _batch_values(batch, "asset_id")}
            if batch_assets != {asset}:
                raise CareFullScaleError("Parquet asset identity differs from the event manifest")
            split = _batch_values(batch, "train_test")
            status = [_normalise_status(value) for value in _batch_values(batch, "status_type_id")]
            row_ids = _batch_values(batch, "id")
            all_values = _feature_matrix(batch, features, np)
            all_quality_masks = _quality_mask_matrix(
                row_ids=row_ids,
                feature_columns=features,
                mask_index=mask_index,
                np=np,
            )
            evidence = _batch_status_evidence(
                state=status_state,
                row_ids=row_ids,
                splits=split,
                statuses=status,
                feature_values=all_values,
                feature_columns=features,
                status_signal_columns=status_signals,
            )
            decisions = tuple(
                evaluate_status_point(
                    farm=farm,
                    split=str(split_value),
                    status_id=status_value,
                    trusted_status_ids=FULL_SCALE_TRAIN_STATUS_IDS,
                    disagreement_run_length=item.disagreement_run_length,
                    corroborated_by_signal=item.corroborated_by_signal,
                )
                for split_value, status_value, item in zip(
                    split,
                    status,
                    evidence,
                    strict=True,
                )
            )
            train_mask = np.asarray(
                [
                    str(split_value) == "train" and decision.model_usable
                    for split_value, decision in zip(split, decisions, strict=True)
                ],
                dtype=bool,
            )
            scanned_rows += len(split)
            batch_trusted_rows = sum(
                str(split_value) == "train" and status_value in FULL_SCALE_TRAIN_STATUS_IDS
                for split_value, status_value in zip(split, status, strict=True)
            )
            batch_retained_rows = sum(
                str(split_value) == "train"
                and status_value not in FULL_SCALE_TRAIN_STATUS_IDS
                and decision.model_usable
                for split_value, status_value, decision in zip(
                    split,
                    status,
                    decisions,
                    strict=True,
                )
            )
            batch_masked_rows = sum(
                str(split_value) == "train" and not decision.model_usable
                for split_value, decision in zip(split, decisions, strict=True)
            )
            if not bool(train_mask.any()):
                for moments in (totals[farm], asset_moments):
                    moments.trusted_train_rows += batch_trusted_rows
                    moments.retained_disagreement_train_rows += batch_retained_rows
                    moments.masked_status_train_rows += batch_masked_rows
                trusted_rows += batch_trusted_rows
                retained_disagreement_rows += batch_retained_rows
                masked_status_rows += batch_masked_rows
                continue
            values = all_values[train_mask]
            quality_masks = all_quality_masks[train_mask]
            finite = np.isfinite(values) & ~quality_masks
            safe = np.where(finite, values, 0.0)
            counts = finite.sum(axis=0, dtype=np.int64)
            sums = safe.sum(axis=0, dtype=np.float64)
            sum_squares = np.square(safe).sum(axis=0, dtype=np.float64)
            quality_counts = quality_masks.sum(axis=0, dtype=np.int64)
            batch_quality_masked_rows = int(quality_masks.any(axis=1).sum())
            batch_quality_masked_values = int(quality_counts.sum())
            selected_rows = int(train_mask.sum())
            for moments in (totals[farm], asset_moments):
                moments.counts += counts
                moments.sums += sums
                moments.sum_squares += sum_squares
                moments.quality_mask_counts += quality_counts
                moments.status_usable_train_rows += selected_rows
                moments.trusted_train_rows += batch_trusted_rows
                moments.retained_disagreement_train_rows += batch_retained_rows
                moments.masked_status_train_rows += batch_masked_rows
                moments.quality_masked_train_rows += batch_quality_masked_rows
            status_usable_rows += selected_rows
            trusted_rows += batch_trusted_rows
            retained_disagreement_rows += batch_retained_rows
            masked_status_rows += batch_masked_rows
            quality_masked_rows += batch_quality_masked_rows
            quality_masked_values += batch_quality_masked_values
        if not trim_process_resident_memory():
            raise CareFullScaleError(
                "full-scale evaluator could not trim training-pass resident memory"
            )
    return (
        totals,
        assets,
        {
            "elapsed_seconds": time.perf_counter() - started,
            "scanned_row_count": scanned_rows,
            "status_usable_train_row_count": status_usable_rows,
            "trusted_train_row_count": trusted_rows,
            "retained_disagreement_train_row_count": retained_disagreement_rows,
            "masked_status_train_row_count": masked_status_rows,
            "quality_masked_train_row_count": quality_masked_rows,
            "quality_masked_train_feature_value_count": quality_masked_values,
            "peak_record_batch_bytes": peak_batch,
            "peak_arrow_allocated_bytes": peak_arrow,
        },
    )


def _build_fold_profiles(
    *,
    totals: Mapping[str, _Moments],
    assets: Mapping[tuple[str, str], _Moments],
    features_by_farm: Mapping[str, tuple[str, ...]],
    import_manifest: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    np = importlib.import_module("numpy")
    profiles: dict[tuple[str, str], dict[str, Any]] = {}
    for (farm, asset), own in sorted(assets.items()):
        total = totals[farm]
        counts = total.counts - own.counts
        if bool((counts <= 0).any()):
            missing = [
                feature
                for feature, count in zip(features_by_farm[farm], counts.tolist(), strict=True)
                if int(count) <= 0
            ]
            raise CareFullScaleError(
                f"held-out {farm}:{asset} leaves features without finite train data: {missing[:5]}"
            )
        sums = total.sums - own.sums
        sum_squares = total.sum_squares - own.sum_squares
        quality_mask_counts = total.quality_mask_counts - own.quality_mask_counts
        means = sums / counts
        variances = np.maximum(0.0, (sum_squares / counts) - np.square(means))
        standard_deviations = np.sqrt(variances)
        standard_deviations = np.where(standard_deviations > 1e-12, standard_deviations, 1.0)
        if not bool(np.isfinite(means).all()) or not bool(np.isfinite(standard_deviations).all()):
            raise CareFullScaleError("full-scale fold profile is non-finite")
        quality_mask_lineage = _quality_mask_lineage(
            import_manifest,
            farm,
            excluded_asset_id=asset,
        )
        quality_mask_policy = _quality_mask_policy(features_by_farm[farm])
        profiles[(farm, asset)] = {
            "farm": farm,
            "held_out_asset_id": asset,
            "training_asset_ids": sorted(
                item_asset
                for item_farm, item_asset in assets
                if item_farm == farm and item_asset != asset
            ),
            "status_usable_train_row_count": (
                total.status_usable_train_rows - own.status_usable_train_rows
            ),
            "trusted_train_row_count": total.trusted_train_rows - own.trusted_train_rows,
            "retained_disagreement_train_row_count": (
                total.retained_disagreement_train_rows - own.retained_disagreement_train_rows
            ),
            "masked_status_train_row_count": (
                total.masked_status_train_rows - own.masked_status_train_rows
            ),
            "quality_masked_train_row_count": (
                total.quality_masked_train_rows - own.quality_masked_train_rows
            ),
            "quality_masked_train_feature_value_count": int(quality_mask_counts.sum()),
            "quality_masked_train_feature_counts": quality_mask_counts.tolist(),
            "quality_mask_policy_sha256": quality_mask_policy["policy_sha256"],
            "training_quality_mask_lineage_sha256": quality_mask_lineage["lineage_sha256"],
            "training_quality_mask_event_count": quality_mask_lineage["event_count"],
            "finite_counts": counts.tolist(),
            "means": means.tolist(),
            "standard_deviations": standard_deviations.tolist(),
            "imputation": "leave-one-asset-out-feature-mean-for-nonfinite-or-quality-masked-v2",
            "prediction_truth_used": False,
        }
    return profiles


def _model_id(farm: str) -> str:
    return f"care-{farm.lower()}-within-farm-loao-zscore-v1"


def _threshold_policy() -> ThresholdPolicy:
    return ThresholdPolicy(
        value=FULL_SCALE_THRESHOLD,
        version=FULL_SCALE_THRESHOLD_VERSION,
        calibration_split="train",
        calibration_run_id=FULL_SCALE_THRESHOLD_RUN_ID,
        strategy="preregistered-from-a-minimal-train-only-before-full-scale",
    )


def _model_selection(farm: str, asset: str) -> ModelSelectionProvenance:
    return ModelSelectionProvenance(
        training_run_id=f"care-v6-{farm.lower()}-loao-{_safe_token(asset)}-train-v2",
        feature_selection_split="not-used",
        early_stopping_split="not-used",
        hyperparameter_selection_split="not-used",
    )


def _build_prediction(
    *,
    event: Mapping[str, Any],
    output_root: Path,
    features: tuple[str, ...],
    status_signal_columns: tuple[str, ...],
    profile: Mapping[str, Any],
    feature_set_sha256: str,
    quality_contract_sha256: str,
    license_metadata: Mapping[str, Any],
    approval_lineage: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    np = importlib.import_module("numpy")
    arrow = importlib.import_module("pyarrow")
    farm = str(event["farm"])
    asset = str(event["source_asset_id"])
    means = np.asarray(profile["means"], dtype=np.float64)
    standard_deviations = np.asarray(profile["standard_deviations"], dtype=np.float64)
    points: list[PredictionPoint] = []
    point_input_evidence: list[tuple[int, int, int, float]] = []
    quality_mask_artifact = _load_quality_mask_artifact(output_root, event)
    quality_mask_index = _quality_mask_index(quality_mask_artifact, features)
    baseline_arrow = int(arrow.total_allocated_bytes())
    peak_batch = 0
    peak_arrow = 0
    started = time.perf_counter()
    status_state = StatusSequenceState(farm, FULL_SCALE_TRAIN_STATUS_IDS)
    columns = ("time_stamp", "id", "train_test", "status_type_id", *features)
    for batch in _iter_event_batches(output_root, event, columns):
        peak_batch = max(peak_batch, int(batch.nbytes))
        peak_arrow = max(
            peak_arrow,
            max(0, int(arrow.total_allocated_bytes()) - baseline_arrow),
        )
        split = _batch_values(batch, "train_test")
        row_ids = _batch_values(batch, "id")
        statuses = _batch_values(batch, "status_type_id")
        all_values = _feature_matrix(batch, features, np)
        all_quality_masks = _quality_mask_matrix(
            row_ids=row_ids,
            feature_columns=features,
            mask_index=quality_mask_index,
            np=np,
        )
        status_evidence = _batch_status_evidence(
            state=status_state,
            row_ids=row_ids,
            splits=split,
            statuses=statuses,
            feature_values=all_values,
            feature_columns=features,
            status_signal_columns=status_signal_columns,
        )
        prediction_positions = [
            index for index, value in enumerate(split) if str(value) == "prediction"
        ]
        if not prediction_positions:
            continue
        values = all_values[prediction_positions]
        quality_masks = all_quality_masks[prediction_positions]
        finite = np.isfinite(values)
        usable = finite & ~quality_masks
        imputed = np.where(usable, values, means)
        scores = np.max(np.abs((imputed - means) / standard_deviations), axis=1)
        ignored_mask_values = np.where(finite, values, means)
        ignored_mask_scores = np.max(
            np.abs((ignored_mask_values - means) / standard_deviations),
            axis=1,
        )
        if not bool(np.isfinite(scores).all()):
            raise CareFullScaleError("full-scale model emitted a non-finite score")
        timestamps = _batch_values(batch, "time_stamp")
        for score_index, batch_index in enumerate(prediction_positions):
            timestamp = _timestamp_text(timestamps[batch_index])
            evidence = status_evidence[batch_index]
            points.append(
                PredictionPoint(
                    source_row_id=int(row_ids[batch_index]),
                    source_timestamp=timestamp,
                    anonymous_time=timestamp,
                    anomaly_score=float(scores[score_index]),
                    status_id=_normalise_status(statuses[batch_index]),
                    disagreement_run_length=evidence.disagreement_run_length,
                    corroborated_by_signal=evidence.corroborated_by_signal,
                    corroboration_signal_count=evidence.corroboration_signal_count,
                    corroboration_finite_signal_count=(evidence.corroboration_finite_signal_count),
                    corroboration_inactive_or_invalid_signal_count=(
                        evidence.corroboration_inactive_or_invalid_signal_count
                    ),
                )
            )
            quality_masked_count = int(quality_masks[score_index].sum())
            nonfinite_count = int((~finite[score_index]).sum())
            point_input_evidence.append(
                (
                    quality_masked_count,
                    nonfinite_count,
                    int((~usable[score_index]).sum()),
                    float(ignored_mask_scores[score_index]),
                )
            )
    if len(points) != int(event["split_counts"]["prediction"]):
        raise CareFullScaleError("full-scale prediction count differs from the import manifest")
    artifact = build_prediction_artifact(
        PredictionBatch(
            event_id=int(event["event_id"]),
            farm=farm,
            source_asset_id=asset,
            points=tuple(points),
            model_id=_model_id(farm),
            model_version=FULL_SCALE_MODEL_VERSION,
            deployment_id=None,
            feature_set_version=FEATURE_SET_VERSION,
            feature_set_sha256=feature_set_sha256,
            quality_rule_version=QUALITY_RULE_VERSION,
            quality_contract_sha256=quality_contract_sha256,
            model_selection_provenance=_model_selection(farm, asset),
        ),
        _threshold_policy(),
        trusted_status_ids=FULL_SCALE_TRAIN_STATUS_IDS,
        status_signal_columns=status_signal_columns,
        license_metadata=license_metadata,
        approval_lineage=approval_lineage,
    )
    artifact_payload = {
        key: value for key, value in artifact.items() if key != "prediction_artifact_sha256"
    }
    artifact_points = artifact_payload.get("points")
    if not isinstance(artifact_points, list) or len(artifact_points) != len(point_input_evidence):
        raise CareFullScaleError("full-scale prediction input evidence shape is invalid")
    for raw_point, input_evidence in zip(artifact_points, point_input_evidence, strict=True):
        if not isinstance(raw_point, dict):
            raise CareFullScaleError("full-scale prediction point is not mutable")
        raw_point.update(
            {
                "quality_masked_feature_count": input_evidence[0],
                "nonfinite_feature_count": input_evidence[1],
                "imputed_feature_count": input_evidence[2],
                "quality_mask_ignored_anomaly_score": input_evidence[3],
                "quality_mask_score_delta": (float(raw_point["anomaly_score"]) - input_evidence[3]),
                "quality_mask_changed_binary_prediction": (
                    (float(raw_point["anomaly_score"]) > FULL_SCALE_THRESHOLD)
                    != (input_evidence[3] > FULL_SCALE_THRESHOLD)
                ),
            }
        )
    event_quality = event["quality"]
    mask_reference = event_quality["mask"]
    selected_feature_columns = set(features)
    selected_mask_range_count = sum(
        1
        for item in quality_mask_artifact["quality_mask_ranges"]
        if str(item["source_column"]) in selected_feature_columns
    )
    artifact_payload.update(
        {
            "quality_mask_policy": _quality_mask_policy(features),
            "quality_mask_reference": dict(mask_reference),
            "quality_mask_range_count": int(quality_mask_artifact["mask_count"]),
            "selected_quality_mask_range_count": selected_mask_range_count,
            "quality_masked_prediction_feature_value_count": sum(
                evidence[0] for evidence in point_input_evidence
            ),
            "quality_masked_prediction_point_count": sum(
                evidence[0] > 0 for evidence in point_input_evidence
            ),
            "nonfinite_prediction_feature_value_count": sum(
                evidence[1] for evidence in point_input_evidence
            ),
            "imputed_prediction_feature_value_count": sum(
                evidence[2] for evidence in point_input_evidence
            ),
            "quality_mask_score_changed_prediction_point_count": sum(
                float(point["quality_mask_score_delta"]) != 0.0 for point in artifact_points
            ),
            "quality_mask_binary_decision_changed_point_count": sum(
                bool(point["quality_mask_changed_binary_prediction"]) for point in artifact_points
            ),
            "quality_mask_max_absolute_score_delta": max(
                (abs(float(point["quality_mask_score_delta"])) for point in artifact_points),
                default=0.0,
            ),
            "quality_mask_mean_absolute_score_delta": (
                sum(abs(float(point["quality_mask_score_delta"])) for point in artifact_points)
                / len(artifact_points)
            ),
            "quality_mask_impact_interpretation": (
                "counterfactual ignores prediction-time masks while retaining the same "
                "mask-trained fold; training-profile changes are proven separately by "
                "raw fold recomputation"
            ),
        }
    )
    artifact = {
        **artifact_payload,
        "prediction_artifact_sha256": _canonical_hash(artifact_payload),
    }
    verify_prediction_artifact(artifact)
    if not trim_process_resident_memory():
        raise CareFullScaleError(
            "full-scale evaluator could not trim prediction-pass resident memory"
        )
    return artifact, {
        "elapsed_seconds": time.perf_counter() - started,
        "prediction_point_count": len(points),
        "peak_record_batch_bytes": peak_batch,
        "peak_arrow_allocated_bytes": peak_arrow,
    }
