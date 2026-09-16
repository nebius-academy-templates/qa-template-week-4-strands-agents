"""Invocation guardrails not supplied to graph nodes by Strands."""

from __future__ import annotations

from strands.hooks import BeforeInvocationEvent, BeforeModelCallEvent, HookRegistry


class ModelCallLimit:
    """Stop one agent invocation before it exceeds its model-call budget."""

    def __init__(self, maximum: int = 120) -> None:
        self.maximum = maximum
        self.calls = 0

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(BeforeModelCallEvent, self.before_model)

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        self.calls = 0

    def before_model(self, event: BeforeModelCallEvent) -> None:
        if self.calls >= self.maximum:
            event.cancel = "Stage model-call limit reached; no execution result was inferred."
            return
        self.calls += 1
