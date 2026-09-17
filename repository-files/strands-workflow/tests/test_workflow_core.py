"""Offline checks for the supplied starter graph and its stop branches."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from state import Assessment, CoverageDecision, Implementation  # noqa: E402
from workflow import validated_gap_handoff  # noqa: E402
from workflow_harness import TARGET, TEST_PATH, WorkflowHarness  # noqa: E402


@pytest.fixture(autouse=True)
def prevent_external_connections(monkeypatch):
    original_connect = socket.socket.connect

    def offline_connect(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("Offline test attempted an external connection")

    monkeypatch.setattr(socket.socket, "connect", offline_connect)


@pytest.fixture
def harness(tmp_path):
    return WorkflowHarness(tmp_path)


def test_generation_schema_rejects_coverage_only_status():
    with pytest.raises(ValidationError):
        Implementation(
            status="ALREADY_COVERED",
            target=TARGET,
            summary="Coverage decisions belong to the coverage stage.",
        )


@pytest.mark.parametrize("status", ["BLOCKED", "NEEDS_CLARIFICATION"])
def test_nonready_case_never_invokes_later_stages(harness, status):
    harness.outputs["readiness"].status = status

    result = harness.run()

    assert result["status"] == status
    assert harness.called_stages == ["readiness"]
    assert result["execution_order"] == ["readiness"]
    assert harness.repository.last_run is None
    assert not harness.repository.changed_files
    assert (
        result["next_action"]
        == {
            "BLOCKED": "Resolve the reported blocker before continuing.",
            "NEEDS_CLARIFICATION": "Answer the reported readiness question before continuing.",
        }[status]
    )


def test_verified_model_claim_without_execution_evidence_stops(harness):
    harness.actions["generation"] = None

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["generation"]["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "coverage", "generation"]


def test_gap_handoff_is_bound_to_case_and_sources_without_execution_evidence(harness):
    result = harness.run()

    coverage = result["stages"]["coverage"]
    assert (
        result["stages"]["readiness"]["next_action_or_question"]
        == "Continue to the coverage preflight."
    )
    assert coverage["case_id"] == harness.repository.case_id
    assert coverage["source_fingerprint"] == "original-source"
    assert "evidence" not in coverage
    assert result["next_action"] == (
        "Run the final case-conformance review for tests.ExampleTest.testExample."
    )


def test_source_change_during_coverage_invalidates_gap_before_generation(harness):
    def change_source():
        harness.repository.source_revision = "changed-during-coverage"
        harness.repository.changed_files.add(TEST_PATH)

    harness.actions["coverage"] = change_source

    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage"]
    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["coverage"]["status"] == "NOT_VERIFIED"


def test_gap_handoff_requires_case_and_current_fingerprint(harness):
    valid = {
        "status": "GAP",
        "case_id": harness.repository.case_id,
        "source_fingerprint": harness.repository.source_fingerprint(),
    }

    assert validated_gap_handoff(valid, harness.repository) is True
    assert validated_gap_handoff({**valid, "case_id": "API-9999"}, harness.repository) is False
    assert validated_gap_handoff({**valid, "source_fingerprint": ""}, harness.repository) is False
    harness.repository.source_revision = "new-source"
    assert validated_gap_handoff(valid, harness.repository) is False


def test_generation_evidence_for_another_target_stops_before_repair(harness):
    wrong_target = "tests.OtherApiTest.testOther"
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED", target_status="FAILED", target=wrong_target
    )

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "coverage", "generation"]
    assert result["stages"]["generation"]["target"] == TARGET
    assert result["stages"]["generation"]["evidence"]["target"] == wrong_target
    assert "does not match" in result["stages"]["generation"]["summary"]


def test_verified_exact_repair_completes_the_starter_route(harness):
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED",
        target_status="FAILED",
        revision="failing",
    )

    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage", "generation", "repair"]
    assert result["stages"]["repair"]["status"] == "VERIFIED"
    assert result["status"] == "VERIFIED"
    assert result["stages"]["generation"]["evidence"]["target_status"] == "FAILED"
    assert result["stages"]["repair"]["evidence"]["target_status"] == "VERIFIED"
    assert result["stages"]["repair"]["evidence"]["target"] == TARGET


def test_repair_evidence_for_another_target_cannot_complete_the_case(harness):
    wrong_target = "tests.OtherApiTest.testOther"
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED", target_status="FAILED", revision="failing"
    )
    harness.actions["repair"] = lambda: harness.repository.record_run(
        revision="wrong-target",
        target=wrong_target,
    )

    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage", "generation", "repair"]
    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["generation"]["target"] == TARGET
    assert result["stages"]["repair"]["target"] == TARGET
    assert result["stages"]["repair"]["evidence"]["target"] == wrong_target
    assert result["stages"]["repair"]["status"] == "NOT_VERIFIED"
    assert "does not match the selected target" in result["stages"]["repair"]["summary"]


def test_coverage_id_conflict_stops_as_blocked(harness):
    harness.repository.coverage_targets = {TARGET: "9001"}
    harness.outputs["coverage"] = CoverageDecision(
        status="BLOCKED",
        target=TARGET,
        summary="The Allure ID belongs to a test that does not cover the supplied behavior.",
    )

    result = harness.run()

    assert result["status"] == "BLOCKED"
    assert harness.called_stages == ["readiness", "coverage"]
    assert result["changed_files"] == []
    assert result["evidence"]["status"] == "NOT_VERIFIED"


@pytest.mark.parametrize(
    "coverage_status", ["GAP", "ALREADY_COVERED", "BLOCKED", "NOT_VERIFIED", "FAILED"]
)
def test_other_case_id_semantic_match_routes_generation(harness, coverage_status):
    legacy_target = "tests.LocationApiTest.testResolveCurrentLocation"
    harness.repository.coverage_targets = {legacy_target: "2090"}
    harness.outputs["coverage"] = CoverageDecision(
        status=coverage_status,
        target=legacy_target,
        summary="The other case has semantically equivalent behavior.",
    )

    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage", "generation"]
    assert result["stages"]["coverage"]["status"] == "GAP"
    assert result["stages"]["coverage"]["target"] == ""
    assert "cannot satisfy this case identity" in result["stages"]["coverage"]["summary"]
    assert result["status"] == "VERIFIED"


def test_gap_with_occupied_selected_id_is_blocked_by_host(harness):
    harness.repository.coverage_targets = {TARGET: "9001"}

    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage"]
    assert result["status"] == "BLOCKED"
    assert result["stages"]["coverage"]["target"] == TARGET
    assert "generation cannot create another test" in result["stages"]["coverage"]["summary"]
    assert result["next_action"] == "Resolve the reported blocker before continuing."


def test_product_bug_from_repair_ends_without_another_stage(harness):
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED", target_status="FAILED", revision="failing"
    )
    harness.outputs["repair"].status = "PRODUCT_BUG"
    harness.actions["repair"] = None

    result = harness.run()

    assert result["status"] == "PRODUCT_BUG"
    assert harness.called_stages == ["readiness", "coverage", "generation", "repair"]
    assert result["evidence"]["status"] == "FAILED"


def test_already_covered_without_execution_evidence_is_not_success(harness):
    harness.repository.coverage_targets = {TARGET: "9001"}
    harness.outputs["coverage"] = CoverageDecision(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "coverage"]
    assert result["stages"]["coverage"]["target"] == TARGET
    assert result["stages"]["coverage"]["status"] == "NOT_VERIFIED"
    assert result["changed_files"] == []
    assert result["evidence"]["status"] == "NOT_VERIFIED"


def test_already_covered_with_matching_exact_target_evidence_is_success(harness):
    harness.repository.coverage_targets = {TARGET: "9001"}
    harness.outputs["coverage"] = CoverageDecision(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )
    harness.actions["coverage"] = lambda: harness.repository.record_run(
        revision="original-source", changed=False
    )

    result = harness.run()

    assert result["status"] == "ALREADY_COVERED"
    assert harness.called_stages == ["readiness", "coverage"]
    assert result["changed_files"] == []
    assert result["evidence"]["target"] == TARGET
    assert result["evidence"]["status"] == "VERIFIED"
    assert result["evidence"]["target_status"] == "VERIFIED"


def test_equivalent_existing_test_failure_uses_the_same_repair_route(harness):
    harness.repository.coverage_targets = {TARGET: "9001"}
    harness.outputs["coverage"] = CoverageDecision(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )
    harness.actions["coverage"] = lambda: harness.repository.record_run(
        status="FAILED",
        target_status="FAILED",
        revision="existing-failure",
        changed=False,
    )

    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage", "repair"]
    assert result["stages"]["coverage"]["status"] == "FAILED"
    assert result["stages"]["coverage"]["evidence"]["target"] == TARGET
    assert result["stages"]["repair"]["evidence"]["target"] == TARGET
    assert result["status"] == "VERIFIED"


def test_already_covered_rejects_wrong_target_evidence(harness):
    harness.repository.coverage_targets = {TARGET: "9001"}
    harness.outputs["coverage"] = CoverageDecision(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )
    harness.actions["coverage"] = lambda: harness.repository.record_run(
        revision="original-source",
        changed=False,
        target="tests.OtherApiTest.testOther",
    )

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "coverage"]
    assert result["stages"]["coverage"]["status"] == "NOT_VERIFIED"


def test_already_covered_claim_with_source_changes_is_not_verified(harness):
    harness.repository.coverage_targets = {TARGET: "9001"}
    harness.outputs["coverage"] = CoverageDecision(
        status="ALREADY_COVERED", target=TARGET, summary="The scenario already has a source test."
    )

    def modify_source_without_execution():
        harness.repository.changed_files.add(TEST_PATH)
        harness.repository.current_diff = "synthetic unexecuted source change"

    harness.actions["coverage"] = modify_source_without_execution

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["coverage"]["status"] == "NOT_VERIFIED"
    assert result["changed_files"] == [TEST_PATH]
    assert harness.called_stages == ["readiness", "coverage"]


def test_model_exception_produces_final_not_verified_report(harness):
    def fail_model_call():
        raise RuntimeError("Synthetic model failure")

    harness.actions["readiness"] = fail_model_call

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert result["graph_status"] != "completed"
    assert harness.called_stages == ["readiness"]
    assert harness.repository.last_run is None


def test_stage_reports_include_only_safe_metric_aggregates(harness):
    result = harness.run()

    metrics = result["stages"]["generation"]["metrics"]
    assert metrics["model"] == {
        "cycles": 1,
        "latency_ms": 0,
        "stop_reason": "tool_use",
    }
    assert metrics["tokens"] == {
        "input": 2,
        "output": 3,
        "total": 5,
    }
    assert metrics["tools"]
    tool = next(iter(metrics["tools"].values()))
    assert set(tool) == {
        "calls",
        "successes",
        "errors",
        "duration_ms",
    }
    generation_input = harness.input_for("generation")
    assert '"metrics"' not in generation_input
    stage_report = (harness.repository.output_dir / "generation.json").read_text(encoding="utf-8")
    assert '"metrics"' in stage_report
    assert "messages" not in stage_report
    assert "input_params" not in stage_report


def test_provider_clients_close_inside_graph_invocation(harness):
    result = harness.run()

    assert set(harness.closed_clients) == {
        "readiness",
        "coverage",
        "generation",
        "repair",
        "review",
    }
    assert all(client.close_calls == 1 for client in harness.model_clients.values())
    assert all(client.closed_while_loop_open for client in harness.model_clients.values())
    for stage in result["execution_order"]:
        client = harness.model_clients[stage]
        assert client.use_loop is client.close_loop


@pytest.mark.parametrize("mode", ["failed_branch", "model_exception"])
def test_provider_clients_close_when_workflow_does_not_succeed(harness, mode):
    if mode == "failed_branch":
        harness.outputs["readiness"].status = "BLOCKED"
    else:
        harness.actions["readiness"] = lambda: (_ for _ in ()).throw(
            RuntimeError("Synthetic model failure")
        )

    harness.run()

    assert set(harness.closed_clients) == {
        "readiness",
        "coverage",
        "generation",
        "repair",
        "review",
    }
    assert all(client.close_calls == 1 for client in harness.model_clients.values())
    assert all(client.closed_while_loop_open for client in harness.model_clients.values())
    readiness_client = harness.model_clients["readiness"]
    assert readiness_client.use_loop is readiness_client.close_loop


def test_one_client_close_error_does_not_skip_other_clients_or_change_result(harness):
    harness.close_errors.add("coverage")

    result = harness.run()

    assert result["status"] == "VERIFIED"
    assert set(harness.close_attempts) == {
        "readiness",
        "coverage",
        "generation",
        "repair",
        "review",
    }
    assert "coverage" not in harness.closed_clients
    assert set(harness.closed_clients) == {
        "readiness",
        "generation",
        "repair",
        "review",
    }


def test_cached_ready_status_skips_model_readiness(harness):
    assessment = Assessment(
        status="READY",
        reason_and_evidence="Reused workbook status.",
        next_action_or_question="Generate a test or reuse an equivalent test.",
    )

    result = harness.run(initial_assessment=assessment, readiness_source="workbook_status")

    assert harness.called_stages == ["coverage", "generation"]
    assert result["execution_order"] == ["coverage", "generation"]
    assert result["stages"]["readiness"]["source"] == "workbook_status"
    assert (
        result["stages"]["readiness"]["next_action_or_question"]
        == "Continue to the coverage preflight."
    )
    assert (harness.repository.output_dir / "readiness.json").is_file()


def test_explicit_model_reassessment_is_labeled_in_stage_and_result(harness):
    result = harness.run(readiness_source="model_reassessment")

    assert result["readiness_source"] == "model_reassessment"
    assert result["stages"]["readiness"]["source"] == "model_reassessment"


@pytest.mark.parametrize("status", ["BLOCKED", "NEEDS_CLARIFICATION"])
def test_cached_nonready_status_stops_without_any_model_call(harness, status):
    assessment = Assessment(
        status=status,
        reason_and_evidence="Reused workbook status.",
        next_action_or_question="Reassess only when explicitly requested.",
    )

    result = harness.run(initial_assessment=assessment, readiness_source="workbook_status")

    assert harness.called_stages == []
    assert result["status"] == status
    assert result["graph_status"] == "skipped"
    assert result["execution_order"] == []
    assert (
        result["next_action"]
        == {
            "BLOCKED": "Resolve the reported blocker before continuing.",
            "NEEDS_CLARIFICATION": "Answer the reported readiness question before continuing.",
        }[status]
    )
    assert (harness.repository.output_dir / "readiness.json").is_file()
