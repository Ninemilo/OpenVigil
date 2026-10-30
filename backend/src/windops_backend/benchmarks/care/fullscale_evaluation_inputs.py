"""Bounded, hash-bound full-scale input and quality-mask readers. No truth reads."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

import windops_backend.benchmarks.care.fullscale_import as _fullscale_import
from windops_backend.benchmarks.care.contract import DATASET_ID, DATASET_VERSION, CareContractError
from windops_backend.benchmarks.care.importer import _sha256_file
from windops_backend.benchmarks.care.pipeline import QUALITY_RULE_VERSION
from windops_backend.benchmarks.care.quality import (
    ZERO_RUN_RULE_ID,
    StatusPointEvidence,
    StatusSequenceState,
    status_evidence_policy,
)

FULL_SCALE_QUALITY_MASK_POLICY_VERSION = _fullscale_import.FULL_SCALE_QUALITY_MASK_POLICY_VERSION
FULL_SCALE_STAGE_ORDER = _fullscale_import.FULL_SCALE_STAGE_ORDER
CareFullScaleError = _fullscale_import.CareFullScaleError
_canonical_hash = _fullscale_import._canonical_hash
_is_sha256 = _fullscale_import._is_sha256


def _normalise_status(value: Any) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _timestamp_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    text = str(value)
    if not text:
        raise CareFullScaleError("prediction timestamp must not be empty")
    return text


def _resolve_reference(output_root: Path, reference: Mapping[str, Any]) -> Path:
    relative = reference.get("local_relative_path")
    if not isinstance(relative, str):
        raise CareFullScaleError("full-scale local artifact reference is missing")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise CareFullScaleError("full-scale local artifact path is unsafe")
    path = output_root.joinpath(*pure.parts).resolve(strict=True)
    if not path.is_relative_to(output_root) or _sha256_file(path) != reference.get("file_sha256"):
        raise CareFullScaleError("full-scale local artifact content changed")
    return path


def _load_json_reference(output_root: Path, reference: Mapping[str, Any]) -> Mapping[str, Any]:
    path = _resolve_reference(output_root, reference)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise CareFullScaleError("full-scale JSON artifact root must be an object")
    return raw


def _quality_mask_policy(feature_columns: Sequence[str]) -> dict[str, Any]:
    payload = {
        "policy_version": FULL_SCALE_QUALITY_MASK_POLICY_VERSION,
        "decision": "apply",
        "feature_columns_sha256": _canonical_hash(list(feature_columns)),
        "range_coordinate": "inclusive-source-row-id",
        "training_action": "exclude-masked-feature-values-from-fold-moments",
        "prediction_action": "impute-masked-feature-values-with-fold-training-mean",
        "nonfinite_prediction_action": "impute-with-fold-training-mean",
        "score_stage": "after-mask-and-imputation",
        "ignored_mask_requires_sensitivity_approval": True,
    }
    return {**payload, "policy_sha256": _canonical_hash(payload)}


def _quality_mask_lineage(
    import_manifest: Mapping[str, Any],
    farm: str,
    *,
    excluded_asset_id: str | None = None,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for event in import_manifest["events"]:
        if str(event["farm"]) != farm or (
            excluded_asset_id is not None and str(event["source_asset_id"]) == excluded_asset_id
        ):
            continue
        quality = event.get("quality")
        reference = quality.get("mask") if isinstance(quality, Mapping) else None
        if not isinstance(reference, Mapping):
            raise CareFullScaleError("full-scale event quality mask reference is missing")
        file_sha256 = reference.get("file_sha256")
        source_quality_sha256 = quality.get("source_quality_report_sha256")
        mask_count = quality.get("mask_count")
        if (
            not _is_sha256(file_sha256)
            or not _is_sha256(source_quality_sha256)
            or not isinstance(mask_count, int)
            or isinstance(mask_count, bool)
            or mask_count < 0
        ):
            raise CareFullScaleError("full-scale event quality mask lineage is malformed")
        events.append(
            {
                "event_id": int(event["event_id"]),
                "source_asset_id": str(event["source_asset_id"]),
                "quality_mask_file_sha256": file_sha256,
                "source_quality_report_sha256": source_quality_sha256,
                "mask_range_count": mask_count,
            }
        )
    events.sort(key=lambda item: int(item["event_id"]))
    payload = {
        "farm": farm,
        "excluded_asset_id": excluded_asset_id,
        "event_count": len(events),
        "events": events,
    }
    return {**payload, "lineage_sha256": _canonical_hash(payload)}


def _load_quality_mask_artifact(
    output_root: Path,
    event: Mapping[str, Any],
) -> Mapping[str, Any]:
    quality = event.get("quality")
    reference = quality.get("mask") if isinstance(quality, Mapping) else None
    if not isinstance(reference, Mapping):
        raise CareFullScaleError("full-scale event quality mask reference is missing")
    if not isinstance(quality, Mapping):
        raise CareFullScaleError("full-scale event quality evidence is missing")
    raw = _load_json_reference(output_root, reference)
    ranges = raw.get("quality_mask_ranges")
    if (
        raw.get("schema_version") != "care-v6-quality-mask-artifact-v1"
        or raw.get("dataset_id") != DATASET_ID
        or raw.get("dataset_version") != DATASET_VERSION
        or raw.get("farm") != event.get("farm")
        or raw.get("event_id") != event.get("event_id")
        or raw.get("source_event_file_sha256") != event.get("source_file_sha256")
        or raw.get("source_quality_report_sha256") != quality.get("source_quality_report_sha256")
        or raw.get("quality_rule_version") != QUALITY_RULE_VERSION
        or raw.get("raw_values_modified") is not False
        or not isinstance(ranges, list)
        or raw.get("mask_count") != len(ranges)
        or raw.get("mask_count") != quality.get("mask_count")
    ):
        raise CareFullScaleError("full-scale quality mask artifact identity is invalid")
    previous_end_by_column: dict[str, int] = {}
    for item in ranges:
        if not isinstance(item, Mapping):
            raise CareFullScaleError("full-scale quality mask range is malformed")
        column = item.get("source_column")
        start = item.get("source_row_id_start")
        end = item.get("source_row_id_end")
        length = item.get("length")
        if (
            not isinstance(column, str)
            or not column
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or end < start
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length != end - start + 1
            or item.get("rule_id") != ZERO_RUN_RULE_ID
            or item.get("rule_version") != QUALITY_RULE_VERSION
            or item.get("raw_value_preserved") is not True
            or start <= previous_end_by_column.get(column, -1)
        ):
            raise CareFullScaleError("full-scale quality mask range is invalid")
        previous_end_by_column[column] = end
    return raw


def _quality_mask_index(
    mask_artifact: Mapping[str, Any],
    feature_columns: Sequence[str],
) -> dict[str, tuple[tuple[int, int], ...]]:
    selected = set(feature_columns)
    by_column: dict[str, list[tuple[int, int]]] = {}
    for item in mask_artifact["quality_mask_ranges"]:
        column = str(item["source_column"])
        if column in selected:
            by_column.setdefault(column, []).append(
                (int(item["source_row_id_start"]), int(item["source_row_id_end"]))
            )
    return {column: tuple(ranges) for column, ranges in by_column.items()}


def _quality_mask_matrix(
    *,
    row_ids: Sequence[Any],
    feature_columns: Sequence[str],
    mask_index: Mapping[str, tuple[tuple[int, int], ...]],
    np: Any,
) -> Any:
    rows = np.asarray([int(value) for value in row_ids], dtype=np.int64)
    if len(rows) > 1 and bool((np.diff(rows) <= 0).any()):
        raise CareFullScaleError("full-scale Parquet source row IDs are not strictly increasing")
    result = np.zeros((len(rows), len(feature_columns)), dtype=bool)
    for feature_index, column in enumerate(feature_columns):
        ranges = mask_index.get(column, ())
        if not ranges or not len(rows):
            continue
        starts = np.asarray([start for start, _ in ranges], dtype=np.int64)
        ends = np.asarray([end for _, end in ranges], dtype=np.int64)
        positions = np.searchsorted(starts, rows, side="right") - 1
        has_candidate = positions >= 0
        safe_positions = np.where(has_candidate, positions, 0)
        result[:, feature_index] = has_candidate & (rows <= ends[safe_positions])
    return result


def _feature_matrix(batch: Any, columns: Sequence[str], np: Any) -> Any:
    arrays = []
    for column in columns:
        index = batch.schema.get_field_index(column)
        if index < 0:
            raise CareFullScaleError(f"full-scale Parquet is missing feature {column}")
        arrow_column = batch.column(index)
        try:
            values = np.asarray(arrow_column.to_numpy(zero_copy_only=False), dtype=np.float64)
        except (TypeError, ValueError):
            values = np.asarray(arrow_column.to_pylist(), dtype=np.float64)
        arrays.append(values)
    return np.column_stack(arrays)


def _batch_values(batch: Any, name: str) -> list[Any]:
    index = batch.schema.get_field_index(name)
    if index < 0:
        raise CareFullScaleError(f"full-scale Parquet is missing metadata column {name}")
    return cast(list[Any], batch.column(index).to_pylist())


def _evaluation_column_contracts(
    output_root: Path,
    import_manifest: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    features_by_farm: dict[str, tuple[str, ...]] = {}
    status_signals_by_farm: dict[str, tuple[str, ...]] = {}
    mapping_references = import_manifest.get("mapping_artifacts")
    if not isinstance(mapping_references, Mapping):
        raise CareFullScaleError("full-scale mapping references are malformed")
    for farm in FULL_SCALE_STAGE_ORDER:
        reference = mapping_references.get(farm)
        if not isinstance(reference, Mapping):
            raise CareFullScaleError(f"full-scale farm {farm} mapping reference is missing")
        mapping = _load_json_reference(output_root, reference)
        features = tuple(str(item) for item in mapping.get("approved_model_input_columns", []))
        if not features or len(features) != int(mapping.get("approved_model_input_count", -1)):
            raise CareFullScaleError(f"full-scale farm {farm} feature mapping is invalid")
        raw_mappings = mapping.get("mappings")
        if not isinstance(raw_mappings, list):
            raise CareFullScaleError(f"full-scale farm {farm} mapping rows are invalid")
        semantics_by_column = {
            str(item.get("source_column")): item.get("quality_semantics")
            for item in raw_mappings
            if isinstance(item, Mapping)
        }
        status_signal_values: list[str] = []
        for column in features:
            semantics = semantics_by_column.get(column)
            if (
                isinstance(semantics, Mapping)
                and semantics.get("value_semantics") == "anonymized-rated-power-scaled"
            ):
                status_signal_values.append(column)
        status_signals = tuple(status_signal_values)
        try:
            status_evidence_policy(farm, status_signals)
        except CareContractError as exc:
            raise CareFullScaleError(str(exc)) from exc
        features_by_farm[farm] = features
        status_signals_by_farm[farm] = status_signals
    return features_by_farm, status_signals_by_farm


def _batch_status_evidence(
    *,
    state: StatusSequenceState,
    row_ids: Sequence[Any],
    splits: Sequence[Any],
    statuses: Sequence[Any],
    feature_values: Any,
    feature_columns: Sequence[str],
    status_signal_columns: Sequence[str],
) -> tuple[StatusPointEvidence, ...]:
    signal_indexes = tuple(feature_columns.index(column) for column in status_signal_columns)
    return tuple(
        state.observe(
            source_row_id=int(row_id),
            split=str(split),
            status_id=_normalise_status(status),
            corroboration_signal_values=tuple(
                feature_values[index, position] for position in signal_indexes
            ),
        )
        for index, (row_id, split, status) in enumerate(zip(row_ids, splits, statuses, strict=True))
    )


def _iter_event_batches(
    output_root: Path,
    event: Mapping[str, Any],
    columns: Sequence[str],
    *,
    batch_size: int = 8192,
) -> Any:
    parquet_module = importlib.import_module("pyarrow.parquet")
    path = _resolve_reference(output_root, event["parquet"]["data"])
    parquet_file = parquet_module.ParquetFile(path)
    try:
        yield from parquet_file.iter_batches(batch_size=batch_size, columns=list(columns))
    finally:
        parquet_file.close()
