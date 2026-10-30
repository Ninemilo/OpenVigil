"""Source-only production workflow execution and a separate post-run scoring CLI."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from windops_backend.access_control import attach_access_policy
from windops_backend.config import Settings
from windops_backend.enums import Environment
from windops_backend.models import AgentExecution, Approval, Mission, WorkOrder
from windops_backend.operations.workflow_eval_budget import (
    FrozenRateCard,
    WorkflowBudgetLimits,
    WorkflowEvalBudget,
)
from windops_backend.operations.workflow_eval_contracts import (
    WorkflowCaseInput,
    canonical_sha256,
)
from windops_backend.operations.workflow_eval_database import OwnedEvaluationDatabase
from windops_backend.operations.workflow_eval_inputs import import_case, load_input_bundle
from windops_backend.operations.workflow_eval_observer import PublicCallObserver
from windops_backend.operations.workflow_eval_transport import isolated_workflow_transport
from windops_backend.outbox import process_outbox_event
from windops_backend.services.workflow import advance_mission_to_review
from windops_backend.storage import OutboxEvent

Mode = Literal["software", "provider"]
PRE_REVIEW_NODES = [
    "scada",
    "vibration",
    "knowledge",
    "diagnosis",
    "alternatives",
    "reviews",
    "hitl",
]
MAX_CASE_SECONDS = 240  # Remain inside the production outbox's five-minute claim lease.
AUDIT_SECONDS = 10
CLEANUP_SECONDS = 30
_ACTIVE_PROVIDER_BUDGET: ContextVar[WorkflowEvalBudget | None] = ContextVar(
    "workflow_eval_provider_budget", default=None
)


class EvaluationWorkflowFailure(RuntimeError):
    """Preserve production audit metadata without persisting raw exception inputs."""

    def __init__(self, original: Exception) -> None:
        self.source_error_code = type(original).__name__
        super().__init__(f"workflow_node_failed:{self.source_error_code}")
        context = getattr(original, "windops_public_context", None)
        if isinstance(context, dict):
            # This is the graph's explicitly public, structured failure envelope;
            # the exception message and traceback never enter the outbox ledger.
            self.__dict__["windops_public_context"] = context


def read_json(path: Path) -> Any:
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("evaluation JSON must be a bounded regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def write_new_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def not_executed(case: WorkflowCaseInput, error_code: str) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "input_sha256": canonical_sha256(case),
        "source_snapshot_sha256": case.source.source_snapshot_sha256,
        "as_of": case.as_of.isoformat(),
        "status": "not_executed",
        "error_code": error_code,
        "latency_ms": 0,
        "public_state": {},
        "executions": [],
        "provider_calls": [],
        "approval_count": None,
        "work_order_count": None,
        "database": None,
    }


def source_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    files = {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }
    versions = {
        name: importlib.metadata.version(name)
        for name in ("sqlalchemy", "pydantic", "langgraph", "litellm")
    }
    return {"source_sha256": files, "python": sys.version, "packages": versions}


def evaluation_settings(
    database_url: str, mode: Mode, budget: WorkflowEvalBudget | None
) -> Settings:
    options: dict[str, Any] = {
        "_env_file": None,
        "database_url": database_url,
        "environment": Environment.TEST if mode == "software" else Environment.DEVELOPMENT,
        "schema_bootstrap": False,
        "demo_seed": False,
        "agent_mode": "deterministic" if mode == "software" else "litellm",
        "document_parser_enabled": False,
        "otel_enabled": False,
        "litellm_max_retries": 0,
        "litellm_minimum_diagnosis_confidence": 0.5,
        "knowledge_graph_backend": "neo4j",
    }
    if budget is not None:
        options["litellm_max_output_tokens"] = min(
            32768, budget.limits.max_output_tokens_per_request
        )
    settings = Settings(**options)
    if mode == "provider":
        if budget is None:
            raise ValueError("provider execution requires a finite budget")
        requested = {
            ("completion", settings.reasoning_connection()[0]),
            ("embedding", settings.embedding_model),
        }
        if not requested <= {(rate.kind, rate.model) for rate in budget.rate_card.rates}:
            raise ValueError("the frozen rate card does not cover configured production models")
    return settings


async def _database_result(
    factory: async_sessionmaker[AsyncSession], mission_id: str, event_id: str
) -> dict[str, Any]:
    # This reads only the database created and owned by the evaluator. Production
    # workflow execution itself runs with the case's explicit caller scope.
    async with factory() as session:
        mission = await session.get(Mission, mission_id)
        event = await session.get(OutboxEvent, event_id)
        rows = (
            await session.scalars(
                select(AgentExecution)
                .where(AgentExecution.mission_id == mission_id)
                .order_by(AgentExecution.started_at, AgentExecution.id)
            )
        ).all()
        executions = [
            {
                field: getattr(row, field)
                for field in (
                    "node",
                    "status",
                    "input_refs",
                    "public_output",
                    "tool_calls",
                    "latency_ms",
                    "provider",
                    "model",
                    "token_usage",
                    "evaluation_result",
                    "degradation_policy",
                    "error_code",
                )
            }
            for row in rows
        ]
        return {
            "mission_status": mission.status if mission else None,
            "event_status": event.status if event else None,
            "public_state": mission.public_state if mission else {},
            "executions": executions,
            "approval_count": await session.scalar(select(func.count()).select_from(Approval)),
            "work_order_count": await session.scalar(select(func.count()).select_from(WorkOrder)),
        }


async def execute_case(
    case: WorkflowCaseInput,
    folder: Path,
    artifact_root: Path,
    *,
    mode: Mode,
    admin_url: str | None,
    budget: WorkflowEvalBudget | None,
    observer: PublicCallObserver,
) -> dict[str, Any]:
    if (mode == "provider" and (admin_url is None or budget is None)) or (
        mode == "software" and admin_url is not None
    ):
        raise ValueError("evaluation mode and database/budget must agree")
    if mode == "provider" and (
        os.environ.get("WINDOPS_WORKFLOW_EVAL_CHILD") != "1"
        or _ACTIVE_PROVIDER_BUDGET.get() is not budget
    ):
        raise RuntimeError("provider execution requires the active child transport budget")
    started = perf_counter()
    call_offset = len(observer.calls)
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "input_sha256": canonical_sha256(case),
        "source_snapshot_sha256": case.source.source_snapshot_sha256,
        "as_of": case.as_of.isoformat(),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "mission_id": None,
        "mission_status": None,
        "event_status": None,
        "public_state": {},
        "executions": [],
        "approval_count": None,
        "work_order_count": None,
        "error_code": None,
    }
    database = OwnedEvaluationDatabase(folder, admin_url=admin_url)
    write_new_json(folder / "database-intent.json", database.evidence())
    imported = None
    factory = None
    try:
        preparation_seconds = min(120, budget.remaining_seconds()) if budget else 120
        async with asyncio.timeout(preparation_seconds):
            engine = await database.open()
        write_new_json(folder / "database-opened.json", database.evidence())
        settings = evaluation_settings(database.url, mode, budget)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        seconds = min(MAX_CASE_SECONDS, budget.remaining_seconds()) if budget else MAX_CASE_SECONDS
        async with asyncio.timeout(seconds):
            async with factory() as session, session.begin():
                imported = await import_case(session, case, artifact_root)
            result["mission_id"] = imported.mission_id
            policy = imported.caller_scope

            async def scoped_handler(
                session: AsyncSession, config: Settings, mission_id: str
            ) -> None:
                attach_access_policy(session, policy)
                try:
                    await advance_mission_to_review(session, config, mission_id)
                except Exception as exc:
                    # ValidationError can include the raw provider input. The
                    # production outbox persists str(error), so sanitize at this
                    # boundary while retaining the graph's paid usage/failure audit.
                    raise EvaluationWorkflowFailure(exc) from None

            succeeded = await process_outbox_event(
                factory, settings, imported.analysis_event_id, scoped_handler
            )
            if not succeeded:
                raise RuntimeError("production workflow claim did not complete")
        async with asyncio.timeout(AUDIT_SECONDS):
            result.update(
                await _database_result(factory, imported.mission_id, imported.analysis_event_id)
            )
        if (
            result["mission_status"] != "under_review"
            or result["event_status"] != "succeeded"
            or [row["node"] for row in result["executions"]] != PRE_REVIEW_NODES
            or any(row["status"] != "succeeded" for row in result["executions"])
            or result["approval_count"] != 0
            or result["work_order_count"] != 0
            or result["public_state"].get("requires_human_approval") is not True
            or result["public_state"].get("approved") is not False
        ):
            raise RuntimeError("production workflow boundary verification failed")
        result["status"] = "succeeded"
    except (Exception, BaseExceptionGroup) as exc:
        result["error_code"] = (
            exc.source_error_code
            if isinstance(exc, EvaluationWorkflowFailure)
            else type(exc).__name__
        )
        if factory is not None and imported is not None:
            try:
                async with asyncio.timeout(AUDIT_SECONDS):
                    result.update(
                        await _database_result(
                            factory, imported.mission_id, imported.analysis_event_id
                        )
                    )
            except Exception as audit_error:
                result["audit_error_code"] = type(audit_error).__name__
    finally:
        result["work_latency_ms"] = round((perf_counter() - started) * 1000)
        if budget is not None:
            try:
                budget.check_deadline()
            except Exception as deadline_error:
                result["status"] = "failed"
                result["deadline_error_code"] = type(deadline_error).__name__
        try:
            async with asyncio.timeout(CLEANUP_SECONDS):
                await database.close()
        except (Exception, BaseExceptionGroup) as cleanup_error:
            result["status"] = "failed"
            result["cleanup_error_code"] = type(cleanup_error).__name__
        result["database"] = database.evidence()
        result["provider_calls"] = observer.calls[call_offset:]
        result["latency_ms"] = round((perf_counter() - started) * 1000)
        result["completed_at"] = datetime.now(UTC).isoformat()
        result["budget_after_case"] = budget.snapshot() if budget else None
    return result


async def run_cases(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("WINDOPS_WORKFLOW_EVAL_CHILD") != "1":
        raise RuntimeError("workflow transport instrumentation requires a dedicated child process")
    inputs = load_input_bundle(args.inputs)
    budget = None
    admin_url = None
    if args.mode == "provider":
        if not args.allow_paid_provider or args.rate_card is None or args.budget is None:
            raise ValueError(
                "provider execution requires explicit consent, prices and finite limits"
            )
        admin_url = os.environ.get("WINDOPS_EVAL_ADMIN_DATABASE_URL")
        if not admin_url:
            raise ValueError("provider execution requires an explicit local admin database URL")
        budget = WorkflowEvalBudget(
            FrozenRateCard.model_validate(read_json(args.rate_card)),
            WorkflowBudgetLimits.model_validate(read_json(args.budget)),
        )
        # Validate model identities before database creation or any external call.
        provider_settings = evaluation_settings(admin_url, "provider", budget)
    report: dict[str, Any] = {
        "schema_version": "openvigil.workflow-eval-run.v1",
        "mode": args.mode,
        "input_bundle_sha256": inputs.input_bundle_sha256,
        "started_at": datetime.now(UTC).isoformat(),
        "runtime": source_identity(),
        "boundary": {
            "expert_acceptance": "UNVERIFIED",
            "field_acceptance": "UNVERIFIED",
            "temporal_policy": "source_snapshot_no_clock_rewrite",
            "execution_approval_validity": "not_assessed",
            "software_mode": "deterministic_reasoning_and_embedding_only",
            "labels_available_to_execution": False,
            "time_budget_scope": "preparation_and_work_through_public_audit",
            "cleanup_grace_seconds_per_case": CLEANUP_SECONDS,
        },
        "cases": [],
        "budget": None,
    }
    observer = PublicCallObserver()
    original_completion = original_embedding = None
    budget_token = None
    transport_stack = ExitStack()
    try:
        if budget is not None:
            transport = transport_stack.enter_context(
                isolated_workflow_transport(provider_settings)
            )
            import litellm

            original_completion, original_embedding = litellm.acompletion, litellm.aembedding
            litellm.acompletion = observer.wrap(budget.wrap_completion(transport.completion))
            litellm.aembedding = budget.wrap_embedding(transport.embedding)
            report["transport"] = transport.evidence()
            budget_token = _ACTIVE_PROVIDER_BUDGET.set(budget)
        for index, case in enumerate(inputs.cases):
            if budget is not None and (
                budget.snapshot()["halted"] or budget.remaining_seconds() <= 0
            ):
                result = not_executed(case, "budget_halted")
            else:
                folder = args.output / f"case-{index:03d}"
                folder.mkdir()
                result = await execute_case(
                    case,
                    folder,
                    args.artifact_root,
                    mode=args.mode,
                    admin_url=admin_url,
                    budget=budget,
                    observer=observer,
                )
            report["cases"].append(result)
            write_new_json(args.output / f"case-{index:03d}.json", result)
    except Exception as run_error:
        report["run_error_code"] = type(run_error).__name__
        for case in inputs.cases[len(report["cases"]) :]:
            report["cases"].append(not_executed(case, type(run_error).__name__))
    finally:
        if budget_token is not None:
            _ACTIVE_PROVIDER_BUDGET.reset(budget_token)
        if original_completion is not None:
            litellm.acompletion = original_completion
            litellm.aembedding = original_embedding
        transport_stack.close()
        report["budget"] = budget.snapshot() if budget else None
        report["completed_at"] = datetime.now(UTC).isoformat()
        write_new_json(args.output / "run.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "_worker"):
        run = commands.add_parser(name)
        run.add_argument("--inputs", type=Path, required=True)
        run.add_argument("--output", type=Path, required=True)
        run.add_argument("--artifact-root", type=Path, required=True)
        run.add_argument("--mode", choices=("software", "provider"), default="software")
        run.add_argument("--allow-paid-provider", action="store_true")
        run.add_argument("--rate-card", type=Path)
        run.add_argument("--budget", type=Path)
    score = commands.add_parser("score")
    score.add_argument("--inputs", type=Path, required=True)
    score.add_argument("--labels", type=Path, required=True)
    score.add_argument("--run", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "score":
            # Import and read labels only in the separate, post-execution command.
            from windops_backend.operations.workflow_eval_contracts import load_label_bundle
            from windops_backend.operations.workflow_eval_scoring import score_run

            inputs = load_input_bundle(args.inputs)
            labels = load_label_bundle(args.labels, inputs)
            scored = score_run(inputs, labels, read_json(args.run))
            write_new_json(args.output, scored)
            print(f"Workflow evaluation score saved: {args.output}")
            return 0 if scored.get("passed") is True else 1
        if args.command == "_worker":
            report = asyncio.run(run_cases(args))
            return (
                0
                if not report.get("run_error_code")
                and all(row["status"] == "succeeded" for row in report["cases"])
                else 1
            )
        # Validate source-only inputs before creating the new evidence directory.
        source = load_input_bundle(args.inputs)
        if args.mode == "provider" and (
            not args.allow_paid_provider or args.rate_card is None or args.budget is None
        ):
            raise ValueError(
                "provider execution requires explicit consent, prices and finite limits"
            )
        args.output = args.output.resolve()
        args.output.mkdir(parents=True, exist_ok=False)
        frozen_input = args.output / "inputs.json"
        raw_input = args.inputs.read_bytes()
        if hashlib.sha256(raw_input).hexdigest() != source.input_bundle_sha256:
            raise ValueError("input changed while freezing the evaluation")
        with frozen_input.open("xb") as stream:
            stream.write(raw_input)
        command = [
            sys.executable,
            "-m",
            "windops_backend.operations.workflow_eval",
            "_worker",
            "--inputs",
            str(frozen_input),
            "--output",
            str(args.output),
            "--artifact-root",
            str(args.artifact_root.resolve()),
            "--mode",
            args.mode,
        ]
        for option in ("rate_card", "budget"):
            value = getattr(args, option)
            if value is not None:
                frozen = args.output / (option + ".json")
                write_new_json(frozen, read_json(value))
                command.extend(["--" + option.replace("_", "-"), str(frozen)])
        if args.allow_paid_provider:
            command.append("--allow-paid-provider")
        env = {**os.environ, "WINDOPS_WORKFLOW_EVAL_CHILD": "1"}
        # Provider SDK diagnostics may contain endpoints/headers. Do not persist
        # its raw stdout/stderr; the child writes bounded public evidence instead.
        if args.mode == "provider":
            limits = WorkflowBudgetLimits.model_validate(read_json(args.output / "budget.json"))
            parent_seconds = limits.max_total_seconds + CLEANUP_SECONDS * len(source.cases) + 150
        else:
            parent_seconds = (
                len(source.cases) * (MAX_CASE_SECONDS + 120 + AUDIT_SECONDS * 2 + CLEANUP_SECONDS)
                + 60
            )
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=parent_seconds,
            )
        except subprocess.TimeoutExpired:
            write_new_json(
                args.output / "parent-timeout.json",
                {
                    "status": "failed",
                    "error_code": "child_deadline_exhausted",
                    "cleanup_status": "UNVERIFIED",
                    "qualification": "UNVERIFIED",
                },
            )
            return 2
        if not (args.output / "run.json").is_file() and not (args.output / "failure.json").exists():
            write_new_json(
                args.output / "failure.json",
                {
                    "status": "failed",
                    "error_code": "worker_did_not_write_report",
                    "exit_code": completed.returncode,
                },
            )
        print(f"Workflow evaluation evidence saved: {args.output}")
        return completed.returncode
    except Exception as exc:
        if args.command == "_worker" and args.output.is_dir():
            if not (args.output / "run.json").exists():
                # Source-valid setup failures still retain the entire denominator.
                inputs = load_input_bundle(args.inputs)
                now = datetime.now(UTC).isoformat()
                write_new_json(
                    args.output / "run.json",
                    {
                        "schema_version": "openvigil.workflow-eval-run.v1",
                        "mode": args.mode,
                        "input_bundle_sha256": inputs.input_bundle_sha256,
                        "started_at": now,
                        "completed_at": now,
                        "cases": [not_executed(case, type(exc).__name__) for case in inputs.cases],
                        "budget": None,
                        "setup_error_code": type(exc).__name__,
                        "boundary": {
                            "expert_acceptance": "UNVERIFIED",
                            "field_acceptance": "UNVERIFIED",
                        },
                    },
                )
            write_new_json(
                args.output / "failure.json",
                {
                    "status": "failed",
                    "error_code": type(exc).__name__,
                    "qualification": "UNVERIFIED",
                },
            )
        # Pydantic and provider errors can embed inputs or URLs; expose class only.
        print(
            f"Workflow evaluation failed ({type(exc).__name__}); no qualification granted.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
