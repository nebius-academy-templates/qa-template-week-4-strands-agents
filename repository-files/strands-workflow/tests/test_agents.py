"""Model configuration checks that do not call a provider."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents as agents_module
from agents import make_agents, make_model
from safety import ModelCallLimit


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
        make_agents(
            SimpleNamespace(root=tmp_path),
            lambda _role: pytest.fail("model was constructed"),
            "API-9001 case",
        )


def test_anthropic_model_enables_ephemeral_prompt_and_tool_cache(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", "1h")

    model = make_model("anthropic", "synthetic-model")
    cache = model.get_config()["cache_config"]

    assert cache.ttl == "1h"
    assert cache.system_prompt_ttl is True
    assert cache.tools_ttl is True
    assert model.get_config()["params"] == {"output_config": {"effort": "medium"}}


def test_anthropic_cache_ttl_rejects_unsupported_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", "24h")

    with pytest.raises(ValueError, match="must be 5m or 1h"):
        make_model("anthropic", "synthetic-model")


def test_review_is_tool_free_packet_input_with_two_call_limit(monkeypatch, tmp_path):
    for path, content in {
        "AGENTS.md": "# Repository rules\n",
        "agent_docs/AI_POLICY.md": "# AI policy\n",
        "agent_docs/task-automation-readiness-instructions.md": "# Readiness\n",
        ".agents/skills/automate-test-case/SKILL.md": (
            "# Workflow\n\n## 4. Check the test against the test case\nCheck the case.\n"
        ),
    }.items():
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")

    class RepositoryStub:
        root = tmp_path
        case_id = "API-9001"
        last_run = None

    def fake_agent(**kwargs):
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(agents_module, "Agent", fake_agent)
    monkeypatch.setattr(agents_module, "tool", lambda function: function)
    monkeypatch.setattr(
        agents_module.Skill,
        "from_file",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(agents_module, "AgentSkills", lambda **kwargs: SimpleNamespace())

    agents = make_agents(RepositoryStub(), lambda _role: SimpleNamespace(), "API-9001 case")
    review = agents["review"]

    assert review.tools == []
    review_limit = next(hook for hook in review.hooks if isinstance(hook, ModelCallLimit))
    assert review_limit.maximum == 2
    assert any(type(hook).__name__ == "ReviewPacketInput" for hook in review.hooks)
    assert "Use only the host-prepared `_review_packet`" in review.system_prompt
