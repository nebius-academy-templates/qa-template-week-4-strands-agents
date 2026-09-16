"""Batch entry-point checks without provider, repository or Gradle execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as entry


def write_workbook(path: Path, *case_ids: str) -> None:
    workbook = Workbook()
    cases = workbook.active
    cases.title = "Cases"
    cases.append(["Case ID", "Action", "Expected result"])
    for case_id in case_ids:
        cases.append([case_id, f"Call for {case_id}", f"Response for {case_id}"])
    details = workbook.create_sheet("Details")
    details.append(["Case ID", "Requirement"])
    for case_id in case_ids:
        details.append([case_id, f"Detail for {case_id}"])
    workbook.save(path)


def install_runtime_stubs(monkeypatch, statuses: dict[str, str]):
    adapters = []
    agent_sets = []
    calls = []
    models = []
    telemetry = []

    class RepositoryStub:
        def __init__(self, root, output_dir, case_id, api_url):
            self.root = Path(root)
            self.output_dir = Path(output_dir)
            self.case_id = case_id
            self.api_url = api_url
            adapters.append(self)

    class TelemetryStub:
        def __init__(self, export):
            self.export = export
            self.closed = False
            telemetry.append(self)

        def close(self):
            self.closed = True

    def make_model(provider, model):
        instance = object()
        models.append((provider, model, instance))
        return instance

    def make_agents(repository, model_factory):
        agents = {"case_id": repository.case_id, "model": model_factory()}
        agent_sets.append(agents)
        return agents

    def run_workflow(case, repository, agents):
        calls.append((repository.case_id, case, repository, agents))
        result = {
            "case_id": repository.case_id,
            "status": statuses[repository.case_id],
            "changed_files": [f"generated/{repository.case_id}.kt"],
        }
        repository.output_dir.mkdir(parents=True)
        (repository.output_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return result

    monkeypatch.setattr(entry, "Repository", RepositoryStub)
    monkeypatch.setattr(entry, "NativeTelemetry", TelemetryStub)
    monkeypatch.setattr(entry, "make_model", make_model)
    monkeypatch.setattr(entry, "make_agents", make_agents)
    monkeypatch.setattr(entry, "run_workflow", run_workflow)
    monkeypatch.setattr(entry, "uuid4", lambda: SimpleNamespace(hex="batch-id"))
    return adapters, agent_sets, calls, models, telemetry


def test_read_case_extracts_only_the_selected_workbook_rows(tmp_path):
    workbook = tmp_path / "cases.xlsx"
    write_workbook(workbook, "API-1001", "API-1002")

    first = entry.read_case(workbook, "API-1001")
    second = entry.read_case(workbook, "API-1002")

    assert "Call for API-1001" in first
    assert "Detail for API-1001" in first
    assert "API-1002" not in first
    assert "Call for API-1002" in second
    assert "API-1001" not in second


def test_case_selection_rejects_duplicates_and_ambiguous_text_batch(tmp_path):
    workbook = tmp_path / "cases.xlsx"
    text = tmp_path / "case.md"
    write_workbook(workbook, "API-1001")
    text.write_text("Complete API-1001 case", encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        entry.validate_case_selection(["API-1001", "API-1001"], workbook)
    with pytest.raises(ValueError, match="XLSX"):
        entry.validate_case_selection(["API-1001", "API-1002"], text)

    entry.validate_case_selection(["API-1001"], text)


def test_main_accepts_one_or_more_case_ids_in_cli_order(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    workbook = tmp_path / "cases.xlsx"
    write_workbook(workbook, "API-1001", "API-1002")
    captured = {}

    def run_cases(repository, case_path, case_ids, provider, model, api_url, export_otel):
        captured["case_ids"] = case_ids
        report_path = repository / "result.json"
        return {"batch_status": "COMPLETED", "cases": []}, report_path

    monkeypatch.setattr(entry, "run_cases", run_cases)

    assert (
        entry.main(
            [
                "--repo",
                str(repository),
                "--case-file",
                str(workbook),
                "--case-id",
                "API-1002",
                "API-1001",
                "--provider",
                "anthropic",
                "--model",
                "test-model",
            ]
        )
        == 0
    )
    assert captured["case_ids"] == ["API-1002", "API-1001"]


def test_batch_runs_one_text_case(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    case_file = tmp_path / "API-1001.md"
    case_file.write_text("Complete API-1001 case", encoding="utf-8")
    _, _, calls, _, telemetry = install_runtime_stubs(
        monkeypatch,
        {"API-1001": "REVIEWED"},
    )

    report, _ = entry.run_cases(
        repository,
        case_file,
        ["API-1001"],
        "anthropic",
        "test-model",
        "http://127.0.0.1:8080",
    )

    assert [(case_id, case) for case_id, case, *_ in calls] == [
        ("API-1001", "Complete API-1001 case")
    ]
    assert report["batch_status"] == "COMPLETED"
    assert report["processed_case_ids"] == ["API-1001"]
    assert report["remaining_case_ids"] == []
    assert telemetry[0].closed is True


def test_batch_runs_cases_in_order_with_fresh_state_and_separate_reports(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    workbook = tmp_path / "cases.xlsx"
    write_workbook(workbook, "API-1001", "API-1002")
    adapters, agent_sets, calls, models, telemetry = install_runtime_stubs(
        monkeypatch,
        {"API-1001": "REVIEWED", "API-1002": "ALREADY_COVERED"},
    )

    report, report_path = entry.run_cases(
        repository,
        workbook,
        ["API-1001", "API-1002"],
        "anthropic",
        "test-model",
        "http://127.0.0.1:8080",
        export_otel=True,
    )

    assert [case_id for case_id, *_ in calls] == ["API-1001", "API-1002"]
    assert len({id(adapter) for adapter in adapters}) == 2
    assert len({id(agents) for agents in agent_sets}) == 2
    assert len({id(model[2]) for model in models}) == 2
    assert adapters[0].output_dir.name == "001-API-1001"
    assert adapters[1].output_dir.name == "002-API-1002"
    assert all((adapter.output_dir / "result.json").is_file() for adapter in adapters)
    assert report["batch_status"] == "COMPLETED"
    assert report["processed_case_ids"] == ["API-1001", "API-1002"]
    assert report["remaining_case_ids"] == []
    assert report["stopped_after"] is None
    assert [item["status"] for item in report["cases"]] == [
        "REVIEWED",
        "ALREADY_COVERED",
    ]
    assert [item["report"] for item in report["cases"]] == [
        "cases/001-API-1001/result.json",
        "cases/002-API-1002/result.json",
    ]
    assert report["cases"][0]["changed_files"] == ["generated/API-1001.kt"]
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert telemetry[0].export is True
    assert telemetry[0].closed is True


def test_batch_stops_before_constructing_the_next_case_agents(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    workbook = tmp_path / "cases.xlsx"
    write_workbook(workbook, "API-1001", "API-1002")
    adapters, agent_sets, calls, models, telemetry = install_runtime_stubs(
        monkeypatch,
        {"API-1001": "CHANGES_REQUESTED", "API-1002": "REVIEWED"},
    )

    report, _ = entry.run_cases(
        repository,
        workbook,
        ["API-1001", "API-1002"],
        "openai",
        "test-model",
        "http://127.0.0.1:8080",
    )

    assert [case_id for case_id, *_ in calls] == ["API-1001"]
    assert len(adapters) == len(agent_sets) == len(models) == 1
    assert report["batch_status"] == "STOPPED"
    assert report["processed_case_ids"] == ["API-1001"]
    assert report["remaining_case_ids"] == ["API-1002"]
    assert report["stopped_after"] == "API-1001"
    assert telemetry[0].closed is True


def test_all_workbook_cases_are_validated_before_agents_are_constructed(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    workbook = tmp_path / "cases.xlsx"
    write_workbook(workbook, "API-1001")
    constructed = []
    monkeypatch.setattr(entry, "make_agents", lambda *args: constructed.append(args))

    with pytest.raises(ValueError, match="API-1002"):
        entry.run_cases(
            repository,
            workbook,
            ["API-1001", "API-1002"],
            "openai",
            "test-model",
            "http://127.0.0.1:8080",
        )

    assert constructed == []
    assert not (repository / ".agent-state").exists()
