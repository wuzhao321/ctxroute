"""Common campaign interface for the internal and native baseline loops."""

from typing import Protocol

from ctxroute.domain import Task
from ctxroute.orchestrator.engine import RunResult
from ctxroute.tracing.writer import RunWriter


class Runner(Protocol):
    writer: RunWriter

    def run(self, task: Task, seed: int) -> RunResult: ...
