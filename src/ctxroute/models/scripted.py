"""Deterministic test double. Never label its usage, responses or timings as measurements."""

from collections.abc import Sequence

from ctxroute.context.manager import Memory
from ctxroute.domain import ModelRequest, ModelResponse
from ctxroute.tracing.cost import Usage


class ScriptedModel:
    def __init__(self, actions: Sequence[str]) -> None:
        self.actions = iter(actions)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        text = (
            Memory(open_questions=["Synthetic compression fixture"]).text()
            if request.purpose == "compression"
            else next(self.actions)
        )
        return ModelResponse(text, f"synthetic-{request.tier}", Usage(0, 0, 0))
