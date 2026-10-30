"""Full-scale ownership, lazy dependency and real synthetic Parquet boundaries."""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from windops_backend.benchmarks.care import (
    fullscale,
    fullscale_evaluation,
    fullscale_evaluation_compute,
    fullscale_evaluation_evidence,
    fullscale_evaluation_inputs,
    fullscale_import,
)
from windops_backend.benchmarks.care.pipeline import BenchmarkJobCancelled
from windops_backend.benchmarks.care.scoring import CareEventTruth, verify_prediction_artifact

# Reuse the existing synthetic raw CSV/Parquet fixture without copying its data.
fixture_module = importlib.import_module("test_care_fullscale")
EXPECTED = cast(fullscale_import.FullScaleExpectedCounts, fixture_module.EXPECTED)
LIMITS = cast(fullscale_import.FullScaleResourceLimits, fixture_module.LIMITS)

OWNERS = {
    "fullscale_evaluation_inputs": (
        "_normalise_status",
        "_timestamp_text",
        "_resolve_reference",
        "_load_json_reference",
        "_quality_mask_policy",
        "_quality_mask_lineage",
        "_load_quality_mask_artifact",
        "_quality_mask_index",
        "_quality_mask_matrix",
        "_feature_matrix",
        "_batch_values",
        "_evaluation_column_contracts",
        "_batch_status_evidence",
        "_iter_event_batches",
    ),
    "fullscale_evaluation_compute": (
        "_Moments",
        "_new_moments",
        "_quality_mask_impact_summary",
        "_accumulate_moments",
        "_build_fold_profiles",
        "_model_id",
        "_threshold_policy",
        "_model_selection",
        "_build_prediction",
    ),
    "fullscale_evaluation_evidence": (
        "verify_full_scale_prediction_status_evidence",
        "verify_full_scale_prediction_model_inputs",
        "_operational_policy",
        "_verify_operational_policy",
        "_verify_model_package",
        "_evaluate_resource_gate",
        "verify_full_scale_evaluation_manifest",
        "resource_prediction_count",
    ),
    "fullscale_evaluation": (
        "_publish_self_hashed_json",
        "_truth_from_event",
        "build_full_scale_evaluation",
        "register_full_scale_evaluation",
    ),
}


def test_both_legacy_paths_export_all_35_owned_objects_and_shared_defaults() -> None:
    for name, symbols in OWNERS.items():
        owner = importlib.import_module(f"windops_backend.benchmarks.care.{name}")
        for symbol in symbols:
            actual = getattr(owner, symbol)
            assert getattr(fullscale, symbol) is actual
            assert getattr(fullscale_evaluation, symbol) is actual
            assert actual.__module__ == owner.__name__
    for symbol in (
        "CareFullScaleError",
        "CareFullScaleResourceError",
        "FullScaleExpectedCounts",
        "FullScaleResourceLimits",
        "FullScaleEvaluationResult",
        "FullScaleEvaluationRegistration",
        "CARE_V6_FULL_SCALE_COUNTS",
        "DEFAULT_FULL_SCALE_RESOURCE_LIMITS",
    ):
        assert getattr(fullscale, symbol) is getattr(fullscale_import, symbol)
        assert getattr(fullscale_evaluation, symbol) is getattr(fullscale_import, symbol)
    for function in (
        fullscale.build_full_scale_evaluation,
        fullscale.verify_full_scale_evaluation_manifest,
        fullscale.register_full_scale_evaluation,
    ):
        parameters = inspect.signature(function).parameters
        assert parameters["expected_counts"].default is fullscale_import.CARE_V6_FULL_SCALE_COUNTS
        assert (
            parameters["resource_limits"].default
            is fullscale_import.DEFAULT_FULL_SCALE_RESOURCE_LIMITS
        )
    assert inspect.iscoroutinefunction(fullscale.register_full_scale_evaluation)
    assert fullscale.resource_prediction_count() == 281_249


@pytest.mark.parametrize("name", [*OWNERS, "fullscale"])
def test_each_entry_imports_independently_in_a_fresh_interpreter(name: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import windops_backend.benchmarks.care.{name}"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_input_compute_evidence_dependency_direction_and_lazy_scientific_imports() -> None:
    prefix = "windops_backend.benchmarks.care."
    levels = {
        "fullscale_evaluation_inputs": 0,
        "fullscale_evaluation_compute": 1,
        "fullscale_evaluation_evidence": 2,
        "fullscale_evaluation": 3,
        "fullscale": 4,
    }
    for module in (
        fullscale_evaluation_inputs,
        fullscale_evaluation_compute,
        fullscale_evaluation_evidence,
    ):
        tree = ast.parse(inspect.getsource(module))
        imports: list[tuple[str, tuple[str, ...]]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend((alias.name, ()) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                imports.append((node.module or "", tuple(alias.name for alias in node.names)))
        for dependency, symbols in imports:
            assert dependency.split(".")[0] not in {"numpy", "pyarrow"}
            assert dependency not in {
                "windops_backend.services.benchmark_metadata",
                "windops_backend.services.models",
            }
            if dependency.startswith(prefix) and dependency.removeprefix(prefix) in levels:
                assert (
                    levels[dependency.removeprefix(prefix)]
                    < levels[module.__name__.removeprefix(prefix)]
                )
            assert not {
                "FinalCareEvaluator",
                "PredictionTruthVault",
                "CareEventTruth",
            }.intersection(symbols)


def test_truth_is_opened_only_after_all_verified_predictions_are_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, archive, source, quality, trust = fixture_module._fixture(tmp_path)
    output = (tmp_path / "derived").resolve()
    imported = fullscale.build_full_scale_import(
        source,
        quality,
        root,
        output,
        source_archive_path=archive,
        expected_counts=EXPECTED,
        resource_limits=LIMITS,
        trust_anchor=trust,
    )
    reads: list[int] = []
    original = fullscale_evaluation._truth_from_event

    def witnessed_truth(path: Path, event: Mapping[str, Any]) -> CareEventTruth:
        freeze = json.loads(
            (path / "care/v6/reports/full-evaluation/prediction-freeze.json").read_text(
                encoding="utf-8"
            )
        )
        assert freeze["freeze_sha256"] == fullscale._canonical_hash(
            {key: value for key, value in freeze.items() if key != "freeze_sha256"}
        )
        assert freeze["prediction_truth_read"] is False
        assert set(freeze["prediction_artifacts"]) == {
            str(item["event_id"]) for item in imported.manifest["events"]
        }
        for reference in freeze["prediction_artifacts"].values():
            prediction = fullscale_evaluation_inputs._load_json_reference(path, reference)
            verify_prediction_artifact(prediction)
        reads.append(int(event["event_id"]))
        return original(path, event)

    monkeypatch.setattr(fullscale_evaluation, "_truth_from_event", witnessed_truth)
    with pytest.raises(BenchmarkJobCancelled):
        fullscale.build_full_scale_evaluation(
            imported.manifest,
            output,
            job_id="split-cancel",
            stop_after_predictions=1,
            expected_counts=EXPECTED,
            resource_limits=LIMITS,
            trust_anchor=trust,
        )
    assert reads == []
    completed = fullscale.build_full_scale_evaluation(
        imported.manifest,
        output,
        job_id="split-resume",
        expected_counts=EXPECTED,
        resource_limits=LIMITS,
        trust_anchor=trust,
    )
    assert sorted(reads) == sorted(int(event["event_id"]) for event in imported.manifest["events"])
    assert len(reads) == EXPECTED.event_count
    assert completed.manifest["summary"]["prediction_point_count"] == 18
