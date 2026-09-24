"""Check model selection and setup without model calls or product execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository-files/strands-workflow"))

import agents as agents_module
import main as entry
from case_loader import CaseInput


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for name, content in {
        "AGENTS.md": "# Repository rules\n",
        "agent_docs/AI_POLICY.md": "# AI policy\n",
        "agent_docs/task-automation-readiness-instructions.md": "# Readiness\n",
        ".agents/skills/automate-test-case/SKILL.md": (
            "## 4. Check the test against the test case\nCheck the supplied case.\n"
        ),
        ".agents/skills/run-appium-suite/SKILL.md": "# Mobile runner\n",
        "case.md": "# API-9001\nSynthetic test case.\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    state = SimpleNamespace(
        root=tmp_path,
        case=tmp_path / "case.md",
        readiness=None,
        fail_model_number=None,
        models=[],
        model_calls=[],
        workflows=[],
        telemetry_closed=False,
    )

    class ClientStub:
        def __init__(self):
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    def make_model(provider, model_id, settings):
        state.model_calls.append((provider, model_id, settings))
        if len(state.model_calls) == state.fail_model_number:
            raise ValueError("Synthetic model setup failure")
        model = SimpleNamespace(client=ClientStub())
        state.models.append(model)
        return model

    def repository(root, output_dir, case_id, *, skip_implemented=False):
        return SimpleNamespace(
            root=root,
            output_dir=output_dir,
            case_id=case_id,
            layer="mobile" if case_id.startswith("MOB-") else "api",
            skip_implemented=skip_implemented,
        )

    def run_workflow(case, repository, agents, **kwargs):
        state.workflows.append(SimpleNamespace(agents=agents, **kwargs))
        assessment = kwargs["initial_assessment"]
        status = assessment.status if assessment and assessment.status != "READY" else "REVIEWED"
        return {"status": status, "next_action": "Synthetic runtime result."}

    def close_telemetry():
        state.telemetry_closed = True

    monkeypatch.setattr(entry, "make_model", make_model)
    monkeypatch.setattr(entry, "Repository", repository)
    monkeypatch.setattr(entry, "run_workflow", run_workflow)
    monkeypatch.setattr(entry, "has_unfinished_repair", lambda root: False)
    monkeypatch.setattr(
        entry,
        "read_cases",
        lambda path, ids, **kwargs: [CaseInput(ids[0], "Synthetic case", state.readiness)],
    )
    monkeypatch.setattr(
        entry, "NativeTelemetry", lambda **kwargs: SimpleNamespace(close=close_telemetry)
    )
    monkeypatch.setattr(agents_module, "Agent", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(agents_module, "AgentSkills", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(agents_module.Skill, "from_file", lambda *args, **kwargs: SimpleNamespace())
    return state


def cli_args(runtime, provider="openai"):
    return [
        "--repo",
        str(runtime.root),
        "--case-file",
        str(runtime.case),
        "--case-id",
        "API-9001",
        "--provider",
        provider,
        "--model",
        "implementation-model",
    ]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("analysis_override", [None, "analysis-model"])
def test_cli_resolves_paired_models_and_persists_report(runtime, provider, analysis_override):
    args = cli_args(runtime, provider)
    if analysis_override:
        args.extend(["--analysis-model", analysis_override])

    assert entry.main(args) == 0

    analysis_id = analysis_override or (
        "claude-sonnet-5" if provider == "anthropic" else "implementation-model"
    )
    assert runtime.model_calls == [
        (provider, analysis_id, entry.ANALYSIS_SETTINGS),
        (provider, "implementation-model", entry.IMPLEMENTATION_SETTINGS),
    ]
    report_path = next(runtime.root.glob(".agent-state/qa-workflow/*/result.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["models"] == {"analysis": analysis_id, "implementation": "implementation-model"}
    assert report["batch_status"] == "COMPLETED"
    assert runtime.telemetry_closed


@pytest.mark.parametrize("case_id", ["API-9001", "MOB-9001"])
@pytest.mark.parametrize("readiness", [None, "READY"])
def test_factory_pairs_roles_and_can_skip_readiness(runtime, case_id, readiness):
    runtime.readiness = readiness

    report, _ = entry.run_cases(
        runtime.root,
        runtime.case,
        [case_id],
        "openai",
        "implementation-model",
        analysis_model="analysis-model",
    )

    assert report["batch_status"] == "COMPLETED"
    agents = runtime.workflows[0].agents
    assert set(agents) == (
        {"generation", "repair", "review", "readiness"}
        if readiness is None
        else {"generation", "repair", "review"}
    )
    assert agents["review"].model is runtime.models[0]
    assert agents["generation"].model is agents["repair"].model is runtime.models[1]
    if readiness is None:
        assert agents["readiness"].model is agents["review"].model
    assert len({id(agent) for agent in agents.values()}) == len(agents)
    assert len({id(agent.hooks[0]) for agent in agents.values()}) == len(agents)
    assert runtime.workflows[0].readiness_source == (
        "model" if readiness is None else "workbook_status"
    )


@pytest.mark.parametrize("readiness", ["BLOCKED", "NEEDS_CLARIFICATION"])
def test_saved_blocking_readiness_does_not_construct_models(runtime, readiness):
    runtime.readiness = readiness

    report, _ = entry.run_cases(
        runtime.root, runtime.case, ["API-9001"], "openai", "implementation-model"
    )

    assert runtime.model_calls == []
    assert runtime.workflows[0].agents == {}
    assert report["cases"][0]["status"] == readiness
    assert report["batch_status"] == "COMPLETED_WITH_ISSUES"


@pytest.mark.parametrize("failure", ["first_model", "second_model", "agents"])
def test_setup_failure_closes_created_clients_and_retains_error(runtime, monkeypatch, failure):
    if failure == "agents":

        def fail_agents(*args, **kwargs):
            raise ValueError("Synthetic agent setup failure")

        monkeypatch.setattr(entry, "make_agents", fail_agents)
    else:
        runtime.fail_model_number = 1 if failure == "first_model" else 2

    report, report_path = entry.run_cases(
        runtime.root, runtime.case, ["API-9001"], "openai", "implementation-model"
    )

    assert report["batch_status"] == "STOPPED"
    assert report["error"]["phase"] == "case_setup"
    assert report["error"]["type"] == "ValueError"
    assert report["error"]["case_id"] == "API-9001"
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert len(runtime.models) == {"first_model": 0, "second_model": 1, "agents": 2}[failure]
    assert all(model.client.close_calls == 1 for model in runtime.models)
    assert runtime.workflows == []
    assert runtime.telemetry_closed


@pytest.mark.parametrize(
    "flag", ["--generation-model", "--repair-model", "--readiness-model", "--review-model"]
)
def test_cli_rejects_removed_role_flags(runtime, flag):
    with pytest.raises(SystemExit) as error:
        entry.main([*cli_args(runtime), flag, "other-model"])

    assert error.value.code == 2
    assert runtime.model_calls == []


def test_cli_help_exposes_two_providers_and_paired_models(capsys):
    with pytest.raises(SystemExit) as error:
        entry.main(["--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "{anthropic,openai}" in help_text
    assert "--model" in help_text
    assert "--analysis-model" in help_text
    for old_flag in ("--generation-model", "--repair-model", "--readiness-model", "--review-model"):
        assert old_flag not in help_text
