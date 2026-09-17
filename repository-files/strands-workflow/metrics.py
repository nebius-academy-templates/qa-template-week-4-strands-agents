"""Content-free metrics saved with workflow results."""

from __future__ import annotations


def stage_metrics(node) -> dict:
    """Return native metrics without prompts, tool arguments or tool results."""
    result_metrics = getattr(node.result, "metrics", None)
    usage = node.accumulated_usage
    tokens = {
        "input": usage.get("inputTokens", 0),
        "output": usage.get("outputTokens", 0),
        "total": usage.get("totalTokens", 0),
    }
    if "cacheReadInputTokens" in usage:
        tokens["cache_read_input"] = usage["cacheReadInputTokens"]
    if "cacheWriteInputTokens" in usage:
        tokens["cache_write_input"] = usage["cacheWriteInputTokens"]

    tools = {}
    for name, metrics in sorted(getattr(result_metrics, "tool_metrics", {}).items()):
        tools[name] = {
            "calls": metrics.call_count,
            "successes": metrics.success_count,
            "errors": metrics.error_count,
            "duration_ms": round(metrics.total_time * 1000),
        }

    return {
        "duration_ms": node.execution_time,
        "model": {
            "cycles": getattr(result_metrics, "cycle_count", 0),
            "latency_ms": node.accumulated_metrics.get("latencyMs", 0),
            "stop_reason": getattr(node.result, "stop_reason", "unknown"),
        },
        "tokens": tokens,
        "tools": tools,
    }
