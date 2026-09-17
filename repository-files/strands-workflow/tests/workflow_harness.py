"""Offline Strands graph harness shared by foundation and practice checks."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from pydantic import BaseModel
from strands import Agent
from strands.models.model import Model

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import Assessment, Implementation, RepairOutcome, ReviewResult
from workflow import run_workflow

TARGET = "tests.ExampleTest.testExample"
TEST_PATH = "api-tests/src/test/kotlin/tests/ExampleTest.kt"
CASE = "API-9001: verify the documented response for the example request."


class OfflineModel(Model):
    """Emit documented tool-use stream events into the native Agent loop."""

    def __init__(self, stage, output, calls, action=None):
        self.stage = stage
        self.output = output
        self.calls = calls
        self.action = action
        self.config = {"model_id": "synthetic-offline", "context_window_limit": 8192}

    def update_config(self, **config):
        self.config.update(config)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("The native structured-output tool must handle the result")

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({"stage": self.stage, "messages": copy.deepcopy(messages)})
        if self.action:
            self.action()
        schema_name = type(self.output).__name__
        assert any(spec["name"] == schema_name for spec in tool_specs or [])
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": self.stage + "-result", "name": schema_name}}
            }
        }
        yield {
            "contentBlockDelta": {"delta": {"toolUse": {"input": self.output.model_dump_json()}}}
        }
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5}}}


class RepositoryStub:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.case_id = "API-9001"
        self.last_run = None
        self.changed_files = set()
        self.source_revision = "original-source"
        self.current_diff = ""
        self.evidence_reads = 0
        self.on_evidence_read = None

    def current_evidence(self):
        self.evidence_reads += 1
        if self.on_evidence_read:
            self.on_evidence_read(self.evidence_reads)
        if self.last_run is None:
            return {"status": "NOT_VERIFIED", "reason": "No execution evidence"}
        if self.last_run["source_digest"] != self.source_revision:
            return {
                **self.last_run,
                "status": "NOT_VERIFIED",
                "reason": "Sources changed after execution",
            }
        return copy.deepcopy(self.last_run)

    def diff(self):
        return self.current_diff

    def record_run(
        self,
        status="VERIFIED",
        target_status="VERIFIED",
        revision="generated",
        changed=True,
        target=TARGET,
    ):
        self.source_revision = revision
        self.current_diff = f"synthetic diff for {revision}" if changed else ""
        if changed:
            self.changed_files.add(TEST_PATH)
        self.last_run = {
            "status": status,
            "target_status": target_status,
            "target": target,
            "source_digest": revision,
            "report": f"synthetic-{revision}-report.xml",
        }


class WorkflowHarness:
    def __init__(self, output_dir):
        self.repository = RepositoryStub(output_dir)
        self.calls = []
        self.outputs: dict[str, BaseModel] = {
            "readiness": Assessment(
                status="READY",
                reason_and_evidence="The synthetic case has an expected response.",
                next_action_or_question="Generate and execute the exact test.",
            ),
            "generation": Implementation(
                status="VERIFIED", target=TARGET, summary="Test generated."
            ),
            "repair": RepairOutcome(status="VERIFIED", target=TARGET, summary="Test repaired."),
            "review": ReviewResult(complete=True, summary="No findings.", findings=[]),
        }
        self.actions = {
            "generation": self.repository.record_run,
            "repair": lambda: self.repository.record_run(revision="repaired"),
        }

    def run(self):
        agents = {
            stage: Agent(
                model=OfflineModel(stage, output, self.calls, self.actions.get(stage)),
                agent_id=stage,
                structured_output_model=type(output),
                callback_handler=None,
                retry_strategy=None,
            )
            for stage, output in self.outputs.items()
        }
        result = run_workflow(CASE, self.repository, agents)
        assert (
            json.loads((self.repository.output_dir / "result.json").read_text(encoding="utf-8"))
            == result
        )
        return result

    @property
    def called_stages(self):
        return [call["stage"] for call in self.calls]

    def input_for(self, stage):
        return json.dumps(next(call["messages"] for call in self.calls if call["stage"] == stage))
