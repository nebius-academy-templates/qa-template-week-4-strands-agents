"""Local workflow metadata and optional native OpenTelemetry export."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.telemetry import StrandsTelemetry


class Telemetry:
    """Record metadata without prompts, file contents, tool arguments or results."""

    def __init__(self, output_dir: Path, export: bool = False) -> None:
        self.path = Path(output_dir) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._native = None
        if export:
            # Keep model and tool content redacted in the optional native export.
            options = [
                item
                for item in os.getenv("OTEL_SEMCONV_STABILITY_OPT_IN", "").split(",")
                if item and not item.startswith("gen_ai_unredacted_attributes=")
            ]
            options.append("gen_ai_unredacted_attributes=")
            os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = ",".join(options)
            self._native = StrandsTelemetry().setup_otlp_exporter()

    def stage(self, event: str, **data: Any) -> None:
        """Append a metadata event; arbitrary text and payload fields are omitted."""
        allowed = {
            "stage",
            "node",
            "status",
            "source",
            "destination",
            "target",
            "case_id",
            "tool",
            "call_id",
            "model_calls",
            "duration_ms",
            "exception_type",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_write_input_tokens",
            "count",
            "limit",
        }
        record = {"time": datetime.now(UTC).isoformat(), "event": event}
        record.update(
            {
                key: value
                for key, value in data.items()
                if key in allowed and (value is None or isinstance(value, (str, int, float, bool)))
            }
        )
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")

    def hooks(self, stage: str, max_model_calls: int = 120) -> HookProvider:
        """Create per-invocation hooks; this limit does not count repair attempts."""
        return _StageHooks(self, stage, max_model_calls)

    def close(self) -> None:
        """Flush an explicitly enabled exporter before process exit."""
        if self._native is not None:
            self._native.tracer_provider.force_flush(timeout_millis=5000)
            self._native.tracer_provider.shutdown()


class _StageHooks:
    def __init__(self, telemetry: Telemetry, stage: str, max_model_calls: int) -> None:
        self.telemetry = telemetry
        self.stage_name = stage
        self.max_model_calls = max_model_calls
        self.calls = 0
        self.started = 0.0
        self.model_started = 0.0

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(AfterModelCallEvent, self.after_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        self.calls = 0
        self.started = time.monotonic()
        self.telemetry.stage("agent_start", stage=self.stage_name)

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        result = event.result
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", {}) or {}
        self.telemetry.stage(
            "agent_stop",
            stage=self.stage_name,
            status=getattr(result, "stop_reason", "no_result"),
            model_calls=self.calls,
            duration_ms=round((time.monotonic() - self.started) * 1000),
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            total_tokens=usage.get("totalTokens"),
            cache_read_input_tokens=usage.get("cacheReadInputTokens"),
            cache_write_input_tokens=usage.get("cacheWriteInputTokens"),
        )

    def before_model(self, event: BeforeModelCallEvent) -> None:
        if self.calls >= self.max_model_calls:
            event.cancel = "Stage model-call limit reached; no execution result was inferred."
            self.telemetry.stage("model_limit", stage=self.stage_name, limit=self.max_model_calls)
            return
        self.calls += 1
        self.model_started = time.monotonic()
        self.telemetry.stage("model_start", stage=self.stage_name, model_calls=self.calls)

    def after_model(self, event: AfterModelCallEvent) -> None:
        self.telemetry.stage(
            "model_stop",
            stage=self.stage_name,
            model_calls=self.calls,
            duration_ms=round((time.monotonic() - self.model_started) * 1000),
            status="error" if event.exception else "completed",
            exception_type=type(event.exception).__name__ if event.exception else None,
        )

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        self.telemetry.stage(
            "tool_start",
            stage=self.stage_name,
            tool=event.tool_use["name"],
            call_id=event.tool_use["toolUseId"],
        )

    def after_tool(self, event: AfterToolCallEvent) -> None:
        self.telemetry.stage(
            "tool_stop",
            stage=self.stage_name,
            tool=event.tool_use["name"],
            call_id=event.tool_use["toolUseId"],
            status=event.result.get("status", "unknown"),
            duration_ms=round(event.duration * 1000) if event.duration is not None else None,
            exception_type=type(event.exception).__name__ if event.exception else None,
        )
