"""Exercise the real Strands/OpenAI protocol with synthetic HTTP responses only."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import BaseModel
from strands import Agent, tool
from strands.models.anthropic import AnthropicModel
from strands.models.openai import OpenAIModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository-files/strands-workflow"))

from agents import ANALYSIS_SETTINGS, ReviewPacketInput, make_model


class FixtureVerdict(BaseModel):
    """Synthetic final result for the protocol fixture."""

    observed: int


def stream_response(*deltas, finish_reason="stop"):
    """Encode actual Chat Completions SSE, including fragmented function arguments."""
    events = [
        {
            "id": "synthetic-completion",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "synthetic-openai-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        for delta in deltas
    ]
    events.append(
        {
            **events[0],
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
    )
    events.append(
        {
            **events[0],
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=content + "data: [DONE]\n\n",
    )


def tool_response(name, arguments, call_id):
    serialized = json.dumps(arguments)
    split = len(serialized) // 2
    return stream_response(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": serialized[:split]},
                }
            ],
        },
        {"tool_calls": [{"index": 0, "function": {"arguments": serialized[split:]}}]},
        finish_reason="tool_calls",
    )


@pytest.fixture
def mocked_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.test/v1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    def build(handler):
        model = make_model("openai", "synthetic-openai-model", ANALYSIS_SETTINGS)
        client_args = dict(model.client_args)

        def resolve_client_args():
            # Strands closes its SDK client after each turn, so each turn needs a fresh client.
            return {
                **client_args,
                "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                "max_retries": 0,
            }

        monkeypatch.setattr(model, "_resolve_client_args", resolve_client_args)
        return model

    return build


def test_openai_executes_streamed_tool_and_forces_structured_output(mocked_openai):
    requests = []
    executions = []

    @tool
    def double_fixture(value: int) -> int:
        """Double a supplied fixture number without external side effects.

        Args:
            value: Number to double.
        """
        executions.append(value)
        return value * 2

    def respond(request):
        assert str(request.url) == "https://openai.example.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-openai-key"
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return tool_response("double_fixture", {"value": 4}, "fixture-call-1")
        if len(requests) == 2:
            return stream_response({"role": "assistant", "content": "The observed result is 8."})
        if len(requests) == 3:
            return tool_response("FixtureVerdict", {"observed": 8}, "fixture-verdict-1")
        pytest.fail("Unexpected extra model request")

    agent = Agent(
        model=mocked_openai(respond),
        tools=[double_fixture],
        structured_output_model=FixtureVerdict,
        callback_handler=None,
    )

    result = agent("Double the fixture value 4 and return the observed result.")

    assert executions == [4]
    assert result.structured_output == FixtureVerdict(observed=8)
    assert len(requests) == 3
    assert all(request["stream"] is True for request in requests)
    assert all(request["model"] == "synthetic-openai-model" for request in requests)
    calls = [message for message in requests[1]["messages"] if "tool_calls" in message]
    assert calls[0]["tool_calls"] == [
        {
            "id": "fixture-call-1",
            "type": "function",
            "function": {"name": "double_fixture", "arguments": '{"value": 4}'},
        }
    ]
    tool_results = [message for message in requests[1]["messages"] if message["role"] == "tool"]
    assert tool_results == [{"role": "tool", "tool_call_id": "fixture-call-1", "content": "8"}]
    assert requests[2]["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in requests[2]["tools"]] == ["FixtureVerdict"]
    schema = requests[2]["tools"][0]["function"]["parameters"]
    assert schema["properties"]["observed"]["type"] == "integer"
    assert schema["required"] == ["observed"]


def test_openai_mobile_review_keeps_native_image_and_packet(mocked_openai):
    requests = []
    preparations = []
    image_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+aK1sAAAAASUVORK5CYII="
    )
    packet = {"case_id": "MOB-9001", "evidence": {"target": "tests.Fixture.testScenario"}}

    def prepare(case, target):
        preparations.append((case, target))
        return packet, [
            {"text": "UI attachment 0, step 1"},
            {"image": {"format": "png", "source": {"bytes": base64.b64decode(image_base64)}}},
        ]

    repository = SimpleNamespace(
        current_evidence=lambda: {"target": "tests.Fixture.testScenario"},
        prepare_review_input=prepare,
    )

    def respond(request):
        requests.append(json.loads(request.content))
        assert len(requests) == 1
        return tool_response("FixtureVerdict", {"observed": 1}, "fixture-image-verdict")

    agent = Agent(
        model=mocked_openai(respond),
        hooks=[ReviewPacketInput()],
        structured_output_model=FixtureVerdict,
        callback_handler=None,
    )

    result = agent(
        "Untrusted previous stage text.",
        invocation_state={"repository": repository, "case": "MOB-9001 fixture case"},
    )

    assert result.structured_output == FixtureVerdict(observed=1)
    assert preparations == [("MOB-9001 fixture case", "tests.Fixture.testScenario")]
    users = [message for message in requests[0]["messages"] if message["role"] == "user"]
    assert len(users) == 1
    content = users[0]["content"]
    assert json.loads(content[0]["text"]) == {"_review_packet": packet}
    assert content[1] == {"type": "text", "text": "UI attachment 0, step 1"}
    assert content[2]["type"] == "image_url"
    assert content[2]["image_url"]["url"] == f"data:image/png;base64,{image_base64}"
    assert "Untrusted previous stage text" not in json.dumps(requests[0])


def test_openai_authentication_failure_does_not_return_a_verdict(mocked_openai):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            401,
            json={"error": {"message": "Synthetic invalid key", "type": "authentication_error"}},
        )

    agent = Agent(
        model=mocked_openai(respond),
        structured_output_model=FixtureVerdict,
        callback_handler=None,
    )

    with pytest.raises(openai.AuthenticationError, match="Synthetic invalid key"):
        agent("Return a fixture result.")

    assert len(requests) == 1


def test_provider_keys_and_endpoints_are_isolated(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.test/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.example.test")
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", "5m")

    openai_model = make_model("openai", "synthetic-openai-model", ANALYSIS_SETTINGS)
    anthropic_model = make_model("anthropic", "synthetic-anthropic-model", ANALYSIS_SETTINGS)

    assert isinstance(openai_model, OpenAIModel)
    assert isinstance(anthropic_model, AnthropicModel)
    assert openai_model.client_args == {
        "api_key": "synthetic-openai-key",
        "base_url": "https://openai.example.test/v1",
    }
    assert anthropic_model.client.api_key == "synthetic-anthropic-key"
    assert str(anthropic_model.client.base_url).rstrip("/") == "https://anthropic.example.test"
    asyncio.run(anthropic_model.client.close())
    assert "output_config" not in (openai_model.get_config().get("params") or {})


def test_openai_missing_key_does_not_fall_back_to_claude(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")

    with pytest.raises(ValueError, match="Set OPENAI_API_KEY in the environment"):
        make_model("openai", "synthetic-openai-model", ANALYSIS_SETTINGS)
