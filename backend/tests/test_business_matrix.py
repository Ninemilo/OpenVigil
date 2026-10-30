import hashlib
import json
from pathlib import Path

import pytest
import yaml
from scripts.run_business_e2e import summarize_browser_evidence
from scripts.verify_business_matrix import SCENARIOS, verify_business_matrix

COMMIT = "a" * 40


def _reports(root):
    for scenario in SCENARIOS:
        folder = root / scenario
        folder.mkdir(parents=True)
        report = {
            "schema": "openvigil.business-e2e.v1",
            "scenario": scenario,
            "commit": COMMIT,
            "ci_run_id": "123",
            "ci_run_attempt": "1",
            "passed": True,
            "cleanup_passed": True,
            "protected_env_unchanged": True,
            "git_index_unchanged": True,
            "worktree_dirty": False,
            "process_cleanup_errors": [],
            "browser_tests": {
                "tests": 1,
                "passed": 1,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "junit_sha256": "b" * 64,
            },
            "verified_minio_objects": 1,
            "database": {"counts": {"unit_fixture_only": 1}},
        }
        (folder / "report.json").write_text(json.dumps(report))


def _verify(root, result="success"):
    return verify_business_matrix(
        root, commit_sha=COMMIT, run_id="123", run_attempt="1", matrix_result=result
    )


def test_complete_current_attempt_verdicts_pass_aggregation_only(tmp_path):
    _reports(tmp_path)
    assert _verify(tmp_path) == sorted(SCENARIOS)


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", "in_progress"])
def test_matrix_job_failure_is_not_hidden_by_successful_verdicts(tmp_path, result):
    _reports(tmp_path)
    with pytest.raises(ValueError, match="every scenario"):
        _verify(tmp_path, result)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_each_scenario_is_independently_required(tmp_path, scenario):
    _reports(tmp_path)
    (tmp_path / scenario / "report.json").unlink()
    with pytest.raises(ValueError, match="exactly five"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", False),
        ("passed", 1),
        ("cleanup_passed", False),
        ("protected_env_unchanged", False),
        ("git_index_unchanged", False),
        ("commit", "c" * 40),
        ("ci_run_id", "122"),
        ("ci_run_attempt", "0"),
        ("worktree_dirty", True),
        ("worktree_dirty", None),
        ("error", "RuntimeError"),
        ("cleanup_error", "TimeoutExpired"),
        ("process_cleanup_errors", ["failed"]),
        ("verified_minio_objects", 0),
        ("verified_minio_objects", True),
        ("database", {}),
        ("browser_tests", None),
        ("scenario", "maintenance"),
    ],
)
def test_incomplete_stale_dirty_or_failed_verdict_is_rejected(tmp_path, field, value):
    _reports(tmp_path)
    path = tmp_path / "document/report.json"
    report = json.loads(path.read_bytes())
    report[field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        _verify(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tests", 0),
        ("tests", 2),
        ("passed", 0),
        ("passed", True),
        ("failures", 1),
        ("errors", 1),
        ("skipped", 1),
        ("junit_sha256", "unknown"),
    ],
)
def test_browser_summary_cannot_hide_missing_or_skipped_execution(tmp_path, field, value):
    _reports(tmp_path)
    path = tmp_path / "structural/report.json"
    report = json.loads(path.read_bytes())
    report["browser_tests"][field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="browser evidence"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    "data",
    [
        "<testsuite/>",
        "<testsuite><testcase/></testsuite>",
        '<testsuite><testcase name="x"><skipped/></testcase></testsuite>',
        '<testsuite><testcase name="x"><failure/></testcase></testsuite>',
        '<testsuite><testcase name="x"><error/></testcase></testsuite>',
        '<testsuite failures="1"><testcase name="x"/></testsuite>',
        '<testsuite><testcase name="x"/><testcase name="y"/></testsuite>',
        '<!DOCTYPE x><testsuite><testcase name="x"/></testsuite>',
    ],
)
def test_browser_summary_rejects_invalid_raw_junit(tmp_path, data):
    path = tmp_path / "browser.xml"
    path.write_text(data)
    with pytest.raises(ValueError):
        summarize_browser_evidence(path)


def test_browser_summary_hashes_actual_non_skipped_case_bytes(tmp_path):
    raw = b'<testsuites><testsuite><testcase name="unit scenario"/></testsuite></testsuites>'
    path = tmp_path / "browser.xml"
    path.write_bytes(raw)
    assert summarize_browser_evidence(path) == {
        "tests": 1,
        "passed": 1,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "junit_sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_browser_summary_cannot_bypass_entity_rejection_with_multibyte_xml(tmp_path, encoding):
    path = tmp_path / "browser.xml"
    path.write_bytes(
        '<!DOCTYPE testsuite [<!ENTITY hidden "expanded">]>'
        '<testsuite><testcase name="&hidden;"/></testsuite>'.encode(encoding)
    )
    with pytest.raises(ValueError):
        summarize_browser_evidence(path)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_browser_summary_preserves_unicode_and_hashes_original_bytes(tmp_path, encoding):
    path = tmp_path / "browser.xml"
    raw = '<testsuite><testcase name="真实场景"/></testsuite>'.encode(encoding)
    path.write_bytes(raw)
    result = summarize_browser_evidence(path)
    assert result["passed"] == 1 and result["junit_sha256"] == hashlib.sha256(raw).hexdigest()


def test_ci_covers_all_scenarios_and_always_runs_the_required_aggregate():
    workflow = yaml.safe_load((Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    matrix = jobs["business-scenarios"]
    assert matrix["strategy"]["matrix"]["scenario"] == [
        "maintenance",
        "structural",
        "structural-modal",
        "structural-scope",
        "document",
    ]
    assert matrix["strategy"]["fail-fast"] is False
    assert matrix["timeout-minutes"] >= 40
    assert not matrix.get("continue-on-error") and not matrix.get("if")
    steps = matrix["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert 'uv sync --project backend --frozen "${extras[@]}"' in commands
    assert "structural*) extras+=(--extra structural)" in commands
    assert "--require-hashes --torch-backend cpu" in commands
    assert "prepare_business_document_fixture.py" in commands
    assert 'args=(--scenario "${WINDOPS_E2E_SCENARIO}")' in commands
    assert 'pnpm test:e2e:business "${args[@]}"' in commands
    upload = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == ".artifacts/business-e2e/*/report.json"
    assert "${{ matrix.scenario }}" in upload["with"]["name"]
    aggregate = jobs["business-cross-layer-e2e"]
    assert aggregate["name"] == "business-cross-layer-e2e"
    assert aggregate["needs"] == "business-scenarios" and aggregate["if"] == "always()"
    verify = aggregate["steps"][-1]
    assert verify["if"] == "always()"
    assert verify["env"]["BUSINESS_MATRIX_RESULT"] == "${{ needs.business-scenarios.result }}"
    assert '--matrix-result "${BUSINESS_MATRIX_RESULT}"' in verify["run"]
    assert '--run-attempt "${GITHUB_RUN_ATTEMPT}"' in verify["run"]
