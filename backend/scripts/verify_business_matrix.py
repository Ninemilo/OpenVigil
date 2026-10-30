"""Reject missing, failed, skipped, stale or dirty five-scenario CI verdicts."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SCENARIOS = frozenset(
    {"maintenance", "structural", "structural-modal", "structural-scope", "document"}
)


def verify_business_matrix(
    reports: Path, *, commit_sha: str, run_id: str, run_attempt: str, matrix_result: str
) -> list[str]:
    if matrix_result != "success":
        raise ValueError("not every scenario job completed successfully")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha) is None:
        raise ValueError("matrix requires the full checkout commit SHA")
    if not run_id.isdecimal() or not run_attempt.isdecimal():
        raise ValueError("matrix requires the current workflow run and attempt")
    files = list(reports.rglob("report.json"))
    if len(files) != len(SCENARIOS):
        raise ValueError("matrix requires exactly five independent scenario verdicts")
    observed: set[str] = set()
    for path in files:
        if path.is_symlink() or not 0 < path.stat().st_size <= 1024 * 1024:
            raise ValueError("business verdict is linked, empty or oversized")
        report: Any = json.loads(path.read_bytes())
        if not isinstance(report, dict) or report.get("schema") != "openvigil.business-e2e.v1":
            raise ValueError("invalid business verdict schema")
        scenario = report.get("scenario")
        if not isinstance(scenario, str) or scenario not in SCENARIOS or scenario in observed:
            raise ValueError("matrix scenario is unknown or duplicated")
        observed.add(scenario)
        if (report.get("commit"), report.get("ci_run_id"), report.get("ci_run_attempt")) != (
            commit_sha,
            run_id,
            run_attempt,
        ):
            raise ValueError(f"{scenario}: verdict belongs to another commit, workflow or attempt")
        for field in ("passed", "cleanup_passed", "protected_env_unchanged", "git_index_unchanged"):
            if report.get(field) is not True:
                raise ValueError(f"{scenario}: {field} was not verified")
        if (
            report.get("worktree_dirty") is not False
            or report.get("error")
            or report.get("cleanup_error")
        ):
            raise ValueError(f"{scenario}: verdict contains errors or uncommitted source changes")
        if report.get("process_cleanup_errors") != []:
            raise ValueError(f"{scenario}: process cleanup was not verified")
        browser = report.get("browser_tests")
        if (
            not isinstance(browser, dict)
            or any(
                type(browser.get(name)) is not int or browser[name] != value
                for name, value in {
                    "tests": 1,
                    "passed": 1,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                }.items()
            )
            or re.fullmatch(r"[0-9a-f]{64}", str(browser.get("junit_sha256", ""))) is None
        ):
            raise ValueError(f"{scenario}: real non-skipped browser evidence is missing")
        objects = report.get("verified_minio_objects")
        if type(objects) is not int or objects < 1:
            raise ValueError(f"{scenario}: original object roundtrip was not verified")
        if not isinstance(report.get("database"), dict) or not report["database"].get("counts"):
            raise ValueError(f"{scenario}: persisted business evidence is missing")
    if observed != SCENARIOS:
        raise ValueError("matrix does not cover all five scenarios")
    return sorted(observed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    for name in ("commit-sha", "run-id", "run-attempt", "matrix-result"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    scenarios = verify_business_matrix(
        args.reports,
        commit_sha=args.commit_sha,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        matrix_result=args.matrix_result,
    )
    print("verified real business scenarios: " + ", ".join(scenarios))


if __name__ == "__main__":
    main()
