"""Local telemetry checks without a collector or provider call."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import telemetry as telemetry_module
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


@pytest.mark.parametrize(
    "setting",
    [
        "",
        "gen_ai_latest_experimental",
        "gen_ai_unredacted_attributes=gen_ai.system_instructions",
        "gen_ai_unredacted_attributes=,gen_ai_unredacted_attributes=gen_ai.input.messages",
    ],
)
def test_native_export_requires_complete_content_redaction(tmp_path, monkeypatch, setting):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", setting)

    with pytest.raises(ValueError, match="with no attribute allowlist"):
        Telemetry(tmp_path, export=True)


def test_native_export_preserves_explicit_otel_configuration(tmp_path, monkeypatch):
    setting = "gen_ai_latest_experimental,gen_ai_unredacted_attributes="
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", setting)
    calls = []

    class NativeTelemetryStub:
        def setup_otlp_exporter(self):
            calls.append("setup")
            return self

    monkeypatch.setattr(telemetry_module, "StrandsTelemetry", NativeTelemetryStub)

    Telemetry(tmp_path, export=True)

    assert calls == ["setup"]
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == setting
