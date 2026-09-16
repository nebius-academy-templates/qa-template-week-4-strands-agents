"""Offline checks for runtime safety and optional native observability."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import telemetry as telemetry_module
from metrics import stage_metrics
from safety import ModelCallLimit
from telemetry import NativeTelemetry


def test_model_call_limit_cancels_only_calls_beyond_the_limit():
    hook = ModelCallLimit(maximum=2)
    hook.before_invocation(SimpleNamespace())

    first = SimpleNamespace(cancel=None)
    second = SimpleNamespace(cancel=None)
    blocked = SimpleNamespace(cancel=None)
    hook.before_model(first)
    hook.before_model(second)
    hook.before_model(blocked)

    assert first.cancel is None
    assert second.cancel is None
    assert blocked.cancel == ("Stage model-call limit reached; no execution result was inferred.")

    reset = SimpleNamespace(cancel=None)
    hook.before_invocation(SimpleNamespace())
    hook.before_model(reset)
    assert reset.cancel is None


def test_stage_metrics_include_reported_cache_counts_without_tool_payloads():
    tool = SimpleNamespace(
        call_count=2,
        success_count=1,
        error_count=1,
        total_time=0.125,
        tool={"name": "read_file", "input": {"path": "must-not-leak"}},
    )
    result = SimpleNamespace(
        stop_reason="end_turn",
        metrics=SimpleNamespace(cycle_count=3, tool_metrics={"read_file": tool}),
        message={"content": [{"text": "must-not-leak"}]},
    )
    node = SimpleNamespace(
        result=result,
        execution_time=250,
        accumulated_metrics={"latencyMs": 200},
        accumulated_usage={
            "inputTokens": 100,
            "outputTokens": 20,
            "totalTokens": 120,
            "cacheReadInputTokens": 80,
            "cacheWriteInputTokens": 10,
        },
    )

    metrics = stage_metrics(node)

    assert metrics["tokens"]["cache_read_input"] == 80
    assert metrics["tokens"]["cache_write_input"] == 10
    assert metrics["tools"] == {
        "read_file": {"calls": 2, "successes": 1, "errors": 1, "duration_ms": 125}
    }
    assert "must-not-leak" not in repr(metrics)


@pytest.mark.parametrize(
    "setting",
    [
        "",
        "gen_ai_latest_experimental",
        "gen_ai_unredacted_attributes=gen_ai.system_instructions",
        "gen_ai_unredacted_attributes=,gen_ai_unredacted_attributes=gen_ai.input.messages",
    ],
)
def test_native_export_requires_complete_content_redaction(monkeypatch, setting):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", setting)

    with pytest.raises(ValueError, match="with no attribute allowlist"):
        NativeTelemetry(export=True)


def test_native_export_preserves_configuration_and_flushes_traces(monkeypatch):
    setting = "gen_ai_latest_experimental,gen_ai_unredacted_attributes="
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", setting)
    calls = []

    class ProviderStub:
        def force_flush(self, timeout_millis):
            calls.append(("flush", timeout_millis))

        def shutdown(self):
            calls.append(("shutdown",))

    class NativeTelemetryStub:
        def __init__(self):
            self.tracer_provider = ProviderStub()

        def setup_otlp_exporter(self):
            calls.append("setup")
            return self

    monkeypatch.setattr(telemetry_module, "StrandsTelemetry", NativeTelemetryStub)

    telemetry = NativeTelemetry(export=True)
    telemetry.close()

    assert calls == ["setup", ("flush", 5000), ("shutdown",)]
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == setting


def test_disabled_native_export_needs_no_environment(monkeypatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    monkeypatch.setattr(
        telemetry_module,
        "StrandsTelemetry",
        lambda: pytest.fail("Disabled export initialized native telemetry"),
    )

    NativeTelemetry(export=False)
