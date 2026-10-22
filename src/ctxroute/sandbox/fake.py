"""In-memory sandbox test double; does not execute commands or access task repositories."""

from ctxroute.domain import Observation, Task


class FakeSandbox:
    def __init__(self, outputs: dict[str, Observation] | None = None, patch: str = "") -> None:
        self.outputs = outputs or {}
        self.patch_text = patch
        self.commands: list[str] = []
        self.active: set[str] = set()
        self.destroyed: list[str] = []

    def create(self, task: Task) -> str:
        handle = f"synthetic-{task.instance_id}"
        self.active.add(handle)
        return handle

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        if handle not in self.active or timeout <= 0:
            raise ValueError("Invalid fake sandbox handle or timeout")
        self.commands.append(command)
        return self.outputs.get(command, Observation("synthetic output", 0))

    def patch(self, handle: str) -> str:
        if handle not in self.active:
            raise ValueError("Unknown fake handle")
        return self.patch_text

    def destroy(self, handle: str) -> None:
        self.active.remove(handle)
        self.destroyed.append(handle)
