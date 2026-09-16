"""Optional native OpenTelemetry export."""

from __future__ import annotations

import os

from strands.telemetry import StrandsTelemetry

_SEMCONV_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
_REDACT_ALL_SENSITIVE_ATTRIBUTES = "gen_ai_unredacted_attributes="


def _require_redacted_native_export() -> None:
    """Reject native export unless Strands is configured to redact sensitive content."""
    options = {item.strip() for item in os.getenv(_SEMCONV_OPT_IN, "").split(",") if item.strip()}
    redaction_options = {
        item for item in options if item.startswith("gen_ai_unredacted_attributes=")
    }
    if redaction_options != {_REDACT_ALL_SENSITIVE_ATTRIBUTES}:
        raise ValueError(
            "Native OTLP export requires OTEL_SEMCONV_STABILITY_OPT_IN to include "
            "gen_ai_unredacted_attributes= with no attribute allowlist"
        )


class NativeTelemetry:
    """Configure the exporters supplied by Strands without changing the environment."""

    def __init__(self, export: bool = False) -> None:
        self._native = None
        if export:
            _require_redacted_native_export()
            self._native = StrandsTelemetry().setup_otlp_exporter()

    def close(self) -> None:
        """Flush an explicitly enabled trace exporter before process exit."""
        if self._native is None:
            return
        self._native.tracer_provider.force_flush(timeout_millis=5000)
        self._native.tracer_provider.shutdown()
