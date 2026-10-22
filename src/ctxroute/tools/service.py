"""Sandbox lifecycle stays in the orchestrator; the model sees bash and optional recall."""

from dataclasses import asdict
from typing import Any

from ctxroute.domain import Task
from ctxroute.sandbox.base import Sandbox


class SandboxService:
    def __init__(self, backend: Sandbox, tasks: dict[str, Task]) -> None:
        self.backend, self.tasks = backend, tasks

    def create_sandbox(self, instance_id: str) -> str:
        return self.backend.create(self.tasks[instance_id])

    def exec(self, handle: str, cmd: str, timeout: float) -> dict[str, Any]:
        return asdict(self.backend.execute(handle, cmd, timeout))

    def get_patch(self, handle: str) -> str:
        return self.backend.patch(handle)

    def destroy(self, handle: str) -> None:
        self.backend.destroy(handle)
