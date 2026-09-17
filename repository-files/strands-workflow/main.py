"""Run the course QA workflow against an explicitly selected practice checkout."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from uuid import uuid4

from agents import make_agents, make_model
from case_loader import (
    SUPPORTED_SUFFIXES,
    TEXT_SUFFIXES,
    CaseInput,
    read_cases,
    validate_prepared_case,
)
from repository import Repository
from state import Assessment
from telemetry import NativeTelemetry
from workflow import run_workflow, verified_target

ANALYSIS_ROLES = frozenset({"readiness", "coverage", "review"})


def resolve_analysis_model(provider: str, model: str, analysis_model: str | None) -> str:
    """Keep the established single-model behavior outside the Anthropic course setup."""
    if analysis_model:
        return analysis_model
    return "claude-sonnet-5" if provider == "anthropic" else model


def case_succeeded(result: dict) -> bool:
    """Accept deduplicated coverage only with matching target proof."""
    if result.get("status") == "REVIEWED":
        return True
    stages = result.get("stages", {})
    coverage = stages.get("coverage", stages.get("generation", {}))
    return (
        result.get("status") == "ALREADY_COVERED"
        and result.get("changed_files") == []
        and verified_target(result.get("evidence", {}), coverage.get("target", ""))
    )


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
    api_url: str,
    export_otel: bool = False,
    analysis_model: str | None = None,
    prepared_cases: bool = False,
    reassess_readiness: bool = False,
) -> tuple[dict, Path]:
    """Run isolated single-case graphs in the requested order."""
    if prepared_cases and reassess_readiness:
        raise ValueError(
            "Prepared-case preflight and readiness reassessment are mutually exclusive"
        )
    validate_case_selection(case_ids, case_path)
    if prepared_cases and case_path.suffix.lower() != ".xlsx":
        raise ValueError("--prepared-cases requires an XLSX workbook with structured case fields")
    # Validate every selection before the first graph can change repository files.
    cases = read_cases(case_path, case_ids)
    if prepared_cases:
        for case in cases:
            validate_prepared_case(case)
    selected_analysis_model = resolve_analysis_model(provider, model, analysis_model)
    output_dir = repository / ".agent-state" / "qa-workflow" / uuid4().hex
    output_dir.mkdir(parents=True)
    case_results = []
    stopped = False
    telemetry = NativeTelemetry(export=export_otel)
    try:
        for index, case in enumerate(cases, 1):
            case_id = case.case_id
            case_output = output_dir / "cases" / f"{index:03d}-{case_id}"
            adapter = Repository(repository, case_output, case_id, api_url)
            assessment, readiness_source = readiness_for_case(
                case,
                prepared_cases=prepared_cases,
                reassess_readiness=reassess_readiness,
            )
            agents = {}
            if assessment is None or assessment.status == "READY":
                role_models = {
                    role: selected_analysis_model if role in ANALYSIS_ROLES else model
                    for role in ("readiness", "coverage", "generation", "repair", "review")
                }
                agents = make_agents(
                    adapter,
                    lambda role, role_models=role_models: make_model(provider, role_models[role]),
                    case.text,
                    include_readiness=assessment is None,
                )
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
                    "report": (case_output / "result.json").relative_to(output_dir).as_posix(),
                    "changed_files": result.get("changed_files", []),
                }
            )
            if not case_succeeded(result):
                stopped = True
                break
    finally:
        telemetry.close()

    processed = [result["case_id"] for result in case_results]
    complete = not stopped and len(processed) == len(case_ids)
    report = {
        "batch_status": "COMPLETED" if complete else "STOPPED",
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
        "processed_case_ids": processed,
        "remaining_case_ids": case_ids[len(processed) :],
        "stopped_after": None if complete else processed[-1],
        "cases": case_results,
    }
    report_path = output_dir / "result.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report, report_path


def readiness_for_case(
    case: CaseInput,
    *,
    prepared_cases: bool,
    reassess_readiness: bool,
) -> tuple[Assessment | None, str]:
    """Reuse an explicit workbook status unless the caller requests reassessment."""
    if reassess_readiness:
        return None, "model_reassessment"
    if case.readiness_status:
        return (
            Assessment(
                status=case.readiness_status,
                reason_and_evidence=(
                    "Reused the exact readiness status from Case Summary.Automated Test."
                ),
                next_action_or_question=(
                    "Continue to coverage preflight."
                    if case.readiness_status == "READY"
                    else "Reassess only when explicitly requested or the case changes."
                ),
            ),
            "workbook_status",
        )
    if prepared_cases:
        return (
            Assessment(
                status="READY",
                reason_and_evidence=(
                    "Prepared-case mode validated the selected case structure and non-empty "
                    "required fields, then skipped a separate model readiness assessment."
                ),
                next_action_or_question="Continue to coverage preflight.",
            ),
            "prepared_preflight",
        )
    return None, "model"


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
            "Readiness, coverage, and review model; defaults to claude-sonnet-5 for "
            "Anthropic and --model for OpenAI"
        ),
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    readiness = parser.add_mutually_exclusive_group()
    readiness.add_argument(
        "--prepared-cases",
        action="store_true",
        help=(
            "Use deterministic structure validation instead of model readiness for statusless cases"
        ),
    )
    readiness.add_argument(
        "--reassess-readiness",
        action="store_true",
        help="Run model readiness even when the workbook already contains a status",
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
        repository=repository,
        case_path=case_path,
        case_ids=args.case_id,
        provider=args.provider,
        model=args.model,
        api_url=args.api_url,
        export_otel=args.otel,
        analysis_model=args.analysis_model,
        prepared_cases=args.prepared_cases,
        reassess_readiness=args.reassess_readiness,
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
