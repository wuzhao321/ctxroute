from typing import Protocol

from ctxroute.domain import ModelRequest, ModelResponse


class Model(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


class ModelFailure(RuntimeError):
    """Value-free failure; unresolved charges stay reserved."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
