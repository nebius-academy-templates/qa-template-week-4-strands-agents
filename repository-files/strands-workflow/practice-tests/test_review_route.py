"""Acceptance checks for Practice: Add the Review Route.

These checks intentionally fail in the distributed starter. They pass after
the supplied review agent is connected behind fresh verified evidence.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from state import Finding, ReviewResult  # noqa: E402
from workflow_harness import CASE, TARGET, TEST_PATH, WorkflowHarness  # noqa: E402


@pytest.fixture(autouse=True)
def prevent_external_connections(monkeypatch):
    original_connect = socket.socket.connect

    def offline_connect(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("Offline practice check attempted an external connection")

    monkeypatch.setattr(socket.socket, "connect", offline_connect)


@pytest.fixture
def harness(tmp_path):
    return WorkflowHarness(tmp_path)


def test_verified_generation_reaches_review_with_current_handoff(harness):
    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage", "generation", "review"]
    assert result["status"] == "REVIEWED"
    review_input = harness.input_for("review")
    assert CASE in review_input
    assert TARGET in review_input
    assert "synthetic-generated-report.xml" in review_input
    assert "synthetic diff for generated" in review_input
    assert '"metrics"' not in review_input


def test_verified_repair_reaches_review_with_repaired_handoff(harness):
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED", target_status="FAILED", revision="failing"
    )

    result = harness.run()

    assert harness.called_stages == [
        "readiness",
        "coverage",
        "generation",
        "repair",
        "review",
    ]
    assert result["status"] == "REVIEWED"
    review_input = harness.input_for("review")
    assert "synthetic-repaired-report.xml" in review_input
    assert "synthetic diff for repaired" in review_input
    assert "synthetic diff for failing" not in review_input


def test_repair_evidence_for_another_target_never_reaches_review(harness):
    wrong_target = "tests.OtherApiTest.testOther"
    harness.outputs["generation"].status = "FAILED"
    harness.actions["generation"] = lambda: harness.repository.record_run(
        status="FAILED",
        target_status="FAILED",
        revision="failing",
    )
    harness.actions["repair"] = lambda: harness.repository.record_run(
        revision="wrong-target", target=wrong_target
    )

    result = harness.run()

    assert harness.called_stages == ["readiness", "coverage", "generation", "repair"]
    assert result["status"] == "NOT_VERIFIED"
    assert result["stages"]["repair"]["target"] == TARGET
    assert result["stages"]["repair"]["evidence"]["target"] == wrong_target
    assert result["stages"]["repair"]["status"] == "NOT_VERIFIED"


def test_review_finding_becomes_changes_requested(harness):
    harness.outputs["review"] = ReviewResult(
        complete=True,
        summary="The expected response field is not asserted.",
        findings=[
            Finding(
                check="Expected result",
                path=TEST_PATH,
                line=12,
                issue="The assertion checks presence only.",
                impact="An incorrect field value would pass.",
                required_change="Compare the field against the case's expected value.",
            )
        ],
    )

    result = harness.run()

    assert result["status"] == "CHANGES_REQUESTED"
    assert result["stages"]["review"]["findings"][0]["path"] == TEST_PATH


@pytest.mark.parametrize("change", ["source", "evidence"])
def test_stale_or_missing_evidence_blocks_review(harness, change):
    def change_before_second_evidence_read(read_number):
        if read_number == 2:
            if change == "source":
                harness.repository.source_revision = "changed-without-execution"
            else:
                harness.repository.last_run = None

    harness.repository.on_evidence_read = change_before_second_evidence_read

    result = harness.run()

    assert result["status"] == "NOT_VERIFIED"
    assert harness.called_stages == ["readiness", "coverage", "generation"]
    assert result["evidence"]["status"] == "NOT_VERIFIED"
