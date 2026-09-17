"""Run the course QA workflow against an explicitly selected practice checkout."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from uuid import uuid4

from agents import make_agents, make_model
from case_loader import SUPPORTED_SUFFIXES, TEXT_SUFFIXES, read_cases
from repository import Repository
from telemetry import NativeTelemetry
from workflow import run_workflow, verified_target


def case_succeeded(result: dict) -> bool:
    """Accept deduplicated coverage only with matching target proof."""
    if result.get("status") == "REVIEWED":
        return True
    generation = result.get("stages", {}).get("generation", {})
    return (
        result.get("status") == "ALREADY_COVERED"
        and result.get("changed_files") == []
        and verified_target(result.get("evidence", {}), generation.get("target", ""))
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
) -> tuple[dict, Path]:
    """Run isolated single-case graphs in the requested order."""
    validate_case_selection(case_ids, case_path)
    # Validate every selection before the first graph can change repository files.
    cases = read_cases(case_path, case_ids)
    output_dir = repository / ".agent-state" / "qa-workflow" / uuid4().hex
    output_dir.mkdir(parents=True)
    case_results = []
    stopped = False
    telemetry = NativeTelemetry(export=export_otel)
    try:
        for index, (case_id, case) in enumerate(cases, 1):
            case_output = output_dir / "cases" / f"{index:03d}-{case_id}"
            adapter = Repository(repository, case_output, case_id, api_url)
            agents = make_agents(adapter, lambda: make_model(provider, model))
            result = run_workflow(case, adapter, agents)
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
        "requested_case_ids": case_ids,
        "processed_case_ids": processed,
        "remaining_case_ids": case_ids[len(processed) :],
        "stopped_after": None if complete else processed[-1],
        "cases": case_results,
    }
    report_path = output_dir / "result.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--case-file", type=Path, required=True)
    parser.add_argument("--case-id", nargs="+", required=True, metavar="API-NNNN")
    parser.add_argument("--provider", choices=("anthropic", "openai"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--otel", action="store_true", help="Export traces using OTEL_* environment settings"
    )
    args = parser.parse_args(argv)

    repository = args.repo.resolve(strict=True)
    case_path = args.case_file.resolve(strict=True)
    try:
        validate_case_selection(args.case_id, case_path)
    except ValueError as error:
        parser.error(str(error))
    result, report_path = run_cases(
        repository,
        case_path,
        args.case_id,
        args.provider,
        args.model,
        args.api_url,
        args.otel,
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
