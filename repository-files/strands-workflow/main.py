"""Run the QA workflow in an explicitly selected repository."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import runpy
import sys
from pathlib import Path
from uuid import uuid4

from agents import ANALYSIS_SETTINGS, IMPLEMENTATION_SETTINGS, make_agents, make_model
from case_loader import SUPPORTED_SUFFIXES, TEXT_SUFFIXES, CaseInput, read_cases
from repository import Repository
from state import Assessment
from telemetry import NativeTelemetry
from workflow import close_model_clients, run_workflow
from workspace import ensure_safe_path

CASE_OUTCOMES = frozenset(
    {
        "REVIEWED",
        "VERIFIED",
        "BLOCKED",
        "NEEDS_CLARIFICATION",
        "CHANGES_REQUESTED",
        "PRODUCT_BUG",
        "NEEDS_INVESTIGATION",
        "EXHAUSTED",
    }
)


def has_unfinished_repair(repository: Path) -> bool:
    """Read queue state using the installed repair hook's parser and terminal states."""
    repository = repository.resolve(strict=True)
    queue = ensure_safe_path(repository, Path(".agent-state/test_repair.json"))
    if not queue.exists():
        return False
    hook_path = ensure_safe_path(repository, Path(".agents/hooks/test_repair.py"))
    hook = runpy.run_path(str(hook_path))
    hook["configure_project_root"](repository)
    return any(
        item["state"] not in hook["TERMINAL_STATES"] for item in hook["load_queue"]()["items"]
    )


def resolve_analysis_model(provider: str, model: str, analysis_model: str | None) -> str:
    if analysis_model:
        return analysis_model
    return "claude-sonnet-5" if provider == "anthropic" else model


def initial_readiness(
    case: CaseInput,
    prepared_cases: bool,
    reassess_readiness: bool,
) -> tuple[Assessment | None, str]:
    """Choose cached, deterministic, or model readiness without treating TODO as status."""
    if prepared_cases and reassess_readiness:
        raise ValueError("--prepared-cases and --reassess-readiness are mutually exclusive")
    if reassess_readiness:
        return None, "model_reassessment"
    if case.readiness_status:
        next_action = (
            "Automate and execute the selected case."
            if case.readiness_status == "READY"
            else "Keep the recorded status unless an explicit reassessment is requested."
        )
        return (
            Assessment(
                status=case.readiness_status,
                reason_and_evidence=(
                    "Reused the exact readiness status from Case Summary.Automated Test."
                ),
                next_action_or_question=next_action,
            ),
            "workbook_status",
        )
    if prepared_cases:
        return (
            Assessment(
                status="READY",
                reason_and_evidence=(
                    "The prepared case passed deterministic workbook structure validation; "
                    "model-based readiness assessment was intentionally skipped."
                ),
                next_action_or_question="Automate and execute the selected case.",
            ),
            "prepared_preflight",
        )
    return None, "model"


def validate_case_selection(case_ids: list[str], case_path: Path) -> None:
    """Require explicit, ordered and unambiguous case selection."""
    if not case_ids or any(not re.fullmatch(r"API-[0-9]+", case_id) for case_id in case_ids):
        raise ValueError("--case-id must identify API cases, for example API-2007")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("--case-id values must be unique")
    suffix = case_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("--case-file must be an XLSX workbook, Markdown file, or text file")
    if suffix in TEXT_SUFFIXES and len(case_ids) != 1:
        raise ValueError("A Markdown or text case file requires exactly one case ID")


def run_cases(
    repository: Path,
    case_path: Path,
    case_ids: list[str],
    provider: str,
    model: str,
    export_otel: bool = False,
    analysis_model: str | None = None,
    prepared_cases: bool = False,
    reassess_readiness: bool = False,
    skip_implemented: bool = False,
) -> tuple[dict, Path]:
    """Run isolated single-case graphs in the requested order."""
    validate_case_selection(case_ids, case_path)
    if prepared_cases and reassess_readiness:
        raise ValueError("--prepared-cases and --reassess-readiness are mutually exclusive")
    if prepared_cases and case_path.suffix.lower() != ".xlsx":
        raise ValueError("--prepared-cases requires an XLSX workbook with structured case fields")
    # Validate every selection before the first graph can change repository files.
    cases = read_cases(
        case_path,
        case_ids,
        require_complete_fields=prepared_cases,
    )
    output_dir = repository / ".agent-state" / "qa-workflow" / uuid4().hex
    output_dir.mkdir(parents=True)
    case_results = []
    accepted = {"REVIEWED"}
    if skip_implemented:
        accepted.add("ALREADY_IMPLEMENTED")
    stopped = False
    selected_analysis_model = resolve_analysis_model(provider, model, analysis_model)
    report_path = output_dir / "result.json"
    report = {
        "batch_status": "RUNNING",
        "models": {
            "analysis": selected_analysis_model,
            "implementation": model,
        },
        "readiness_mode": (
            "reassess"
            if reassess_readiness
            else "prepared"
            if prepared_cases
            else "reuse_or_assess"
        ),
        "requested_case_ids": case_ids,
        "skip_implemented": skip_implemented,
        "active_case_id": None,
        "cases": case_results,
    }

    def save_report() -> None:
        processed = [result["case_id"] for result in case_results]
        report.update(
            processed_case_ids=processed,
            remaining_case_ids=case_ids[len(processed) :],
            stopped_after=processed[-1] if stopped and processed else None,
            unresolved_case_ids=[
                result["case_id"] for result in case_results if result["status"] not in accepted
            ],
        )
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(report_path)

    def record_error(error: BaseException, phase: str) -> None:
        nonlocal stopped
        stopped = True
        detail = {
            "type": type(error).__name__,
            "message": str(error),
            "case_id": report["active_case_id"],
            "phase": phase,
        }
        report["cleanup_error" if "error" in report else "error"] = detail
        report["batch_status"] = "STOPPED"
        save_report()

    save_report()
    print(f"Batch report: {report_path}", file=sys.stderr, flush=True)
    telemetry = None
    phase = "telemetry_setup"
    try:
        telemetry = NativeTelemetry(export=export_otel)
        for index, case in enumerate(cases, 1):
            phase = "repair_queue"
            if has_unfinished_repair(repository):
                stopped = True
                report["stop_reason"] = "Resolve unfinished repair queue work before continuing."
                break
            case_id = case.case_id
            report["active_case_id"] = case_id
            save_report()
            phase = "case_setup"
            case_output = output_dir / "cases" / f"{index:03d}-{case_id}"
            adapter = Repository(
                repository, case_output, case_id, skip_implemented=skip_implemented
            )
            assessment, readiness_source = initial_readiness(
                case, prepared_cases, reassess_readiness
            )
            agents = {}
            if assessment is None or assessment.status == "READY":
                analysis = implementation = None
                try:
                    analysis = make_model(provider, selected_analysis_model, ANALYSIS_SETTINGS)
                    implementation = make_model(provider, model, IMPLEMENTATION_SETTINGS)
                    agents = make_agents(
                        adapter,
                        analysis_model=analysis,
                        implementation_model=implementation,
                        include_readiness=assessment is None,
                    )
                except BaseException:
                    asyncio.run(close_model_clients((analysis, implementation)))
                    raise
            phase = "workflow"
            result = run_workflow(
                case.text,
                adapter,
                agents,
                initial_assessment=assessment,
                readiness_source=readiness_source,
            )
            case_results.append(
                {
                    "case_id": case_id,
                    "status": result["status"],
                    "next_action": result["next_action"],
                    "report": (case_output / "result.json").relative_to(output_dir).as_posix(),
                    "changed_files": result.get("changed_files", []),
                }
            )
            if result.get("error_type") or result.get("status") not in CASE_OUTCOMES | accepted:
                stopped = True
                report["stop_reason"] = result["next_action"]
            report["active_case_id"] = None
            save_report()
            if stopped:
                break
    except BaseException as error:
        record_error(error, phase)
        if not isinstance(error, Exception):
            raise
    finally:
        if telemetry is not None:
            try:
                telemetry.close()
            except Exception as error:
                record_error(error, "telemetry_close")
        complete = not stopped and len(case_results) == len(case_ids)
        report["batch_status"] = (
            "STOPPED"
            if not complete
            else "COMPLETED_WITH_ISSUES"
            if report["unresolved_case_ids"]
            else "COMPLETED"
        )
        save_report()
    return report, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--case-file", type=Path, required=True)
    parser.add_argument("--case-id", nargs="+", required=True, metavar="API-NNNN")
    parser.add_argument("--provider", choices=("anthropic", "openai"), required=True)
    parser.add_argument("--model", required=True, help="Generation and repair model")
    parser.add_argument(
        "--analysis-model",
        help=(
            "Readiness and review model; defaults to claude-sonnet-5 for "
            "Anthropic and --model for OpenAI"
        ),
    )
    readiness = parser.add_mutually_exclusive_group()
    readiness.add_argument(
        "--prepared-cases",
        action="store_true",
        help="Use deterministic structure validation when a case has no saved readiness status",
    )
    readiness.add_argument(
        "--reassess-readiness",
        action="store_true",
        help="Run model readiness even when the workbook already contains a status",
    )
    parser.add_argument(
        "--skip-implemented",
        action="store_true",
        help="Skip unchanged tests that fully implement their assigned case, without a fresh run",
    )
    parser.add_argument(
        "--otel", action="store_true", help="Export traces using OTEL_* environment settings"
    )
    args = parser.parse_args(argv)

    repository = args.repo.resolve(strict=True)
    case_path = args.case_file.resolve(strict=True)
    try:
        validate_case_selection(args.case_id, case_path)
        if args.prepared_cases and case_path.suffix.lower() != ".xlsx":
            raise ValueError(
                "--prepared-cases requires an XLSX workbook with structured case fields"
            )
    except ValueError as error:
        parser.error(str(error))
    result, report_path = run_cases(
        repository,
        case_path,
        args.case_id,
        args.provider,
        args.model,
        export_otel=args.otel,
        analysis_model=args.analysis_model,
        prepared_cases=args.prepared_cases,
        reassess_readiness=args.reassess_readiness,
        skip_implemented=args.skip_implemented,
    )
    print(
        json.dumps(
            {"status": result["batch_status"], "report": str(report_path)},
            indent=2,
        )
    )
    return 0 if result["batch_status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
