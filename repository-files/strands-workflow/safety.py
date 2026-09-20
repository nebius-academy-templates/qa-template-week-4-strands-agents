"""Invocation guardrails not supplied to graph nodes by Strands."""

from __future__ import annotations

from strands.hooks import BeforeInvocationEvent, BeforeModelCallEvent, HookRegistry


class ModelCallLimitExceeded(RuntimeError):
    """Identify a host budget stop without blaming the model's structured output."""

    def __init__(self, stage: str, calls: int, maximum: int) -> None:
        self.stage = stage
        self.calls = calls
        self.maximum = maximum
        super().__init__(
            f"Model-call limit reached for {stage} ({calls}/{maximum} calls); "
            "the stage did not complete."
        )


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
            raise ModelCallLimitExceeded(event.agent.name, self.calls, self.maximum)
        self.calls += 1
