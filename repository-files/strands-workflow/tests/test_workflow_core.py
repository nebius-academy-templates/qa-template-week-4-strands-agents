"""Offline checks for the supplied starter graph and its stop branches."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from state import Implementation  # noqa: E402
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


@pytest.mark.parametrize("status", ["BLOCKED", "NEEDS_CLARIFICATION"])
def test_nonready_case_never_invokes_later_stages(harness, status):
    harness.outputs["readiness"].status = status

    result = harness.run()

    assert result["status"] == status
    assert harness.called_stages == ["readiness"]
    assert result["execution_order"] == ["readiness"]
    assert harness.repository.last_run is None
    assert not harness.repository.changed_files


def test_verified_model_claim_without_execution_evidence_stops(harness):
    harness.actions["generation"] = None

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["generation"]["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "generation"]


def test_generation_evidence_for_another_target_stops_before_repair(harness):
    wrong_target = "tests.OtherApiTest.testOther"
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED", target_status="FAILED", target=wrong_target
    )

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "generation"]
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

    assert harness.called_stages == ["readiness", "generation", "repair"]
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

    assert harness.called_stages == ["readiness", "generation", "repair"]
    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["generation"]["target"] == TARGET
    assert result["stages"]["repair"]["target"] == TARGET
    assert result["stages"]["repair"]["evidence"]["target"] == wrong_target
    assert result["stages"]["repair"]["status"] == "NOT_VERIFIED"
    assert "does not match the selected target" in result["stages"]["repair"]["summary"]


def test_generation_id_conflict_stops_as_blocked(harness):
    harness.outputs["generation"] = Implementation(
        status="BLOCKED",
        target=TARGET,
        summary="The Allure ID belongs to a test that does not cover the supplied behavior.",
    )
    harness.actions["generation"] = None

    result = harness.run()

    assert result["status"] == "BLOCKED"
    assert harness.called_stages == ["readiness", "generation"]
    assert result["changed_files"] == []
    assert result["evidence"]["status"] == "NOT_VERIFIED"


def test_product_bug_from_repair_ends_without_another_stage(harness):
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED", target_status="FAILED", revision="failing"
    )
    harness.outputs["repair"].status = "PRODUCT_BUG"
    harness.actions["repair"] = None

    result = harness.run()

    assert result["status"] == "PRODUCT_BUG"
    assert harness.called_stages == ["readiness", "generation", "repair"]
    assert result["evidence"]["status"] == "FAILED"


def test_already_covered_without_execution_evidence_is_not_success(harness):
    harness.outputs["generation"] = Implementation(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )
    harness.actions["generation"] = None

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "generation"]
    assert result["stages"]["generation"]["target"] == TARGET
    assert result["stages"]["generation"]["status"] == "NOT_VERIFIED"
    assert result["changed_files"] == []
    assert result["evidence"]["status"] == "NOT_VERIFIED"


def test_already_covered_with_matching_exact_target_evidence_is_success(harness):
    harness.outputs["generation"] = Implementation(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )
    harness.actions["generation"] = lambda: harness.repository.record_run(
        revision="original-source", changed=False
    )

    result = harness.run()

    assert result["status"] == "ALREADY_COVERED"
    assert harness.called_stages == ["readiness", "generation"]
    assert result["changed_files"] == []
    assert result["evidence"]["target"] == TARGET
    assert result["evidence"]["status"] == "VERIFIED"
    assert result["evidence"]["target_status"] == "VERIFIED"


def test_equivalent_existing_test_failure_uses_the_same_repair_route(harness):
    harness.outputs["generation"] = Implementation(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED",
        target_status="FAILED",
        revision="existing-failure",
        changed=False,
    )

    result = harness.run()

    assert harness.called_stages == ["readiness", "generation", "repair"]
    assert result["stages"]["generation"]["status"] == "FAILED"
    assert result["stages"]["generation"]["evidence"]["target"] == TARGET
    assert result["stages"]["repair"]["evidence"]["target"] == TARGET
    assert result["status"] == "VERIFIED"


def test_already_covered_rejects_wrong_target_evidence(harness):
    harness.outputs["generation"] = Implementation(
        status="ALREADY_COVERED",
        target=TARGET,
        summary="The exact scenario already has a source test.",
    )
    harness.actions["generation"] = lambda: harness.repository.record_run(
        revision="original-source",
        changed=False,
        target="tests.OtherApiTest.testOther",
    )

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "generation"]
    assert result["stages"]["generation"]["status"] == "NOT_VERIFIED"


def test_already_covered_claim_with_source_changes_is_not_verified(harness):
    harness.outputs["generation"] = Implementation(
        status="ALREADY_COVERED", target=TARGET, summary="The scenario already has a source test."
    )

    def modify_source_without_execution():
        harness.repository.changed_files.add(TEST_PATH)
        harness.repository.current_diff = "synthetic unexecuted source change"

    harness.actions["generation"] = modify_source_without_execution

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["generation"]["status"] == "NOT_VERIFIED"
    assert result["changed_files"] == [TEST_PATH]
    assert harness.called_stages == ["readiness", "generation"]


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
