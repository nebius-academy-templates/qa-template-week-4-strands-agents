"""Content-free metrics saved with workflow results."""

from __future__ import annotations


def _token_metrics(usage: dict, missing: int | None = 0) -> dict:
    tokens = {
        "input": usage.get("inputTokens", missing),
        "output": usage.get("outputTokens", missing),
        "total": usage.get("totalTokens", missing),
    }
    if "cacheReadInputTokens" in usage:
        tokens["cache_read_input"] = usage["cacheReadInputTokens"]
    if "cacheWriteInputTokens" in usage:
        tokens["cache_write_input"] = usage["cacheWriteInputTokens"]
    return tokens


def _tool_metrics(result_metrics) -> dict:
    tools = {}
    for name, metrics in sorted(getattr(result_metrics, "tool_metrics", {}).items()):
        tools[name] = {
            "calls": metrics.call_count,
            "successes": metrics.success_count,
            "errors": metrics.error_count,
            "duration_ms": round(metrics.total_time * 1000),
        }
    return tools


def stage_metrics(node) -> dict:
    """Return native metrics without prompts, tool arguments or tool results."""
    result_metrics = getattr(node.result, "metrics", None)

    return {
        "duration_ms": node.execution_time,
        "model": {
            "cycles": getattr(result_metrics, "cycle_count", 0),
            "latency_ms": node.accumulated_metrics.get("latencyMs", 0),
            "stop_reason": getattr(node.result, "stop_reason", "unknown"),
        },
        "tokens": _token_metrics(node.accumulated_usage),
        "tools": _tool_metrics(result_metrics),
    }


def partial_stage_metrics(node, event_loop_metrics) -> dict:
    """Keep observed agent counters after failure, without claiming complete usage.

    Strands replaces failed node usage with zero counters. The agent retains
    metrics from completed calls, although a failing call may not report usage.
    Absent metrics remain unknown rather than becoming a zero-cost result.
    """
    usage = getattr(event_loop_metrics, "accumulated_usage", {})
    performance = getattr(event_loop_metrics, "accumulated_metrics", {})
    return {
        "partial": True,
        "duration_ms": getattr(node, "execution_time", None),
        "model": {
            "cycles": getattr(event_loop_metrics, "cycle_count", None),
            "latency_ms": performance.get("latencyMs"),
            "stop_reason": "error",
        },
        "tokens": _token_metrics(usage, missing=None),
        "tools": _tool_metrics(event_loop_metrics),
    }
