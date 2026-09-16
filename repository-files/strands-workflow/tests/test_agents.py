"""Model configuration checks that do not call a provider."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import make_agents, make_model


def test_make_agents_rejects_readiness_instructions_outside_agent_docs(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Repository rules\n", encoding="utf-8")
    policy = tmp_path / "agent_docs/AI_POLICY.md"
    policy.parent.mkdir(parents=True)
    policy.write_text("# AI policy\n", encoding="utf-8")
    misplaced = tmp_path / "task-automation-readiness-instructions.md"
    misplaced.parent.mkdir(parents=True, exist_ok=True)
    misplaced.write_text("# Misplaced readiness instructions\n", encoding="utf-8")

    with pytest.raises(
        FileNotFoundError,
        match="agent_docs/task-automation-readiness-instructions.md is required",
    ):
        make_agents(SimpleNamespace(root=tmp_path), lambda: pytest.fail("model was constructed"))


def test_anthropic_model_enables_ephemeral_prompt_and_tool_cache(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", "1h")

    model = make_model("anthropic", "synthetic-model")
    cache = model.get_config()["cache_config"]

    assert cache.ttl == "1h"
    assert cache.system_prompt_ttl is True
    assert cache.tools_ttl is True


def test_anthropic_cache_ttl_rejects_unsupported_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", "24h")

    with pytest.raises(ValueError, match="must be 5m or 1h"):
        make_model("anthropic", "synthetic-model")
