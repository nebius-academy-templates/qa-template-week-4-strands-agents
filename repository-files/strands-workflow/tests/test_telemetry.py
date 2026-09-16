"""Local telemetry checks without a collector or provider call."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry import Telemetry, _StageHooks


def test_agent_stop_records_cache_usage_without_arbitrary_payload(tmp_path):
    telemetry = Telemetry(tmp_path)
    hooks = _StageHooks(telemetry, "generation", max_model_calls=3)
    hooks.started = time.monotonic()
    result = SimpleNamespace(
        stop_reason="end_turn",
        metrics=SimpleNamespace(
            accumulated_usage={
                "inputTokens": 100,
                "outputTokens": 20,
                "totalTokens": 120,
                "cacheReadInputTokens": 80,
                "cacheWriteInputTokens": 10,
            }
        ),
    )

    hooks.after_invocation(SimpleNamespace(result=result))
    telemetry.stage("synthetic", prompt="must not be stored", tool_result="must not be stored")

    records = [json.loads(line) for line in telemetry.path.read_text().splitlines()]
    assert records[0]["cache_read_input_tokens"] == 80
    assert records[0]["cache_write_input_tokens"] == 10
    assert "prompt" not in records[1]
    assert "tool_result" not in records[1]
