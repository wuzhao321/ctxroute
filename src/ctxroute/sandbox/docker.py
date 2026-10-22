"""Docker command construction and bounded execution; no host shell fallback."""

import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ctxroute.domain import Observation, Task


@dataclass(frozen=True)
class ProcessResult:
    output: str
    code: int
    timed_out: bool = False
    output_limited: bool = False


Executor = Callable[[list[str], float, int], ProcessResult]


def bounded_process(argv: list[str], timeout: float, max_bytes: int) -> ProcessResult:
    """Bound pipe memory and process lifetime; argv never passes through a host shell."""
    if timeout <= 0 or max_bytes < 1:
        raise ValueError("Positive process limits required")
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    )
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    data = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = limited = False
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            for key, _ in selector.select(min(0.1, max(0.0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                room = max_bytes - len(data)
                data.extend(chunk[:room])
                if len(chunk) > room:
                    limited = True
                    break
            if limited:
                break
        if timed_out or limited:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            code = proc.wait()
        return ProcessResult(data.decode(errors="replace"), code, timed_out, limited)
    finally:
        selector.close()
        proc.stdout.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


class DockerSandbox:
    def __init__(
        self, registry: dict[str, str], config: dict[str, Any], executor: Executor = bounded_process
    ) -> None:
        self.registry, self.config, self.executor = registry, config, executor
        self.owned: set[str] = set()
        if config["cpus"] <= 0 or config["pids_limit"] < 1 or config["output_bytes"] < 1:
            raise ValueError("Invalid sandbox resources")

    def _run(self, argv: list[str], timeout: float | None = None) -> ProcessResult:
        return self.executor(
            [self.config["docker_binary"], *argv],
            self.config["control_timeout_seconds"] if timeout is None else timeout,
            self.config["output_bytes"],
        )

    def _check(self, handle: str) -> None:
        if handle not in self.owned:
            raise ValueError("Sandbox handle is not owned by this session")

    def create(self, task: Task) -> str:
        image = self.registry[task.instance_id]
        if re.search(r"(?:@|^)sha256:[a-f0-9]{64}$", image) is None:
            raise ValueError("Require an audited, digest-pinned task image")
        name = "ctxroute-" + uuid4().hex
        argv = [
            "create",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            "ctxroute.managed=true",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.config["pids_limit"]),
            "--memory",
            self.config["memory"],
            "--cpus",
            str(self.config["cpus"]),
            "--workdir",
            self.config["workdir"],
            "--entrypoint",
            "/bin/sleep",
            image,
            "infinity",
        ]
        result = self._run(argv)
        if result.code != 0 or result.timed_out or result.output_limited:
            self._run(["rm", "-f", name])
            raise RuntimeError("Container creation failed")
        self.owned.add(name)
        result = self._run(["start", name])
        if result.code != 0 or result.timed_out:
            self.destroy(name)
            raise RuntimeError("Container start failed")
        return name

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        self._check(handle)
        duration = min(timeout, self.config["command_timeout_seconds"])
        if duration <= 0:
            raise ValueError("Command has no remaining time allowance")
        result = self._run(
            [
                "exec",
                handle,
                "timeout",
                "--signal=TERM",
                "--kill-after=2s",
                f"{duration}s",
                "bash",
                "-lc",
                command,
            ],
            duration + self.config["kill_grace_seconds"],
        )
        timed_out = result.timed_out or result.code in {124, 137}
        if timed_out or result.output_limited:
            # Killing docker's host client alone does not kill the container process.
            # Stop this owned container to guarantee no orphan command remains.
            self._run(["kill", handle])
        return Observation(result.output, result.code, timed_out, result.output_limited)

    def patch(self, handle: str) -> str:
        self._check(handle)
        result = self._run(
            [
                "exec",
                handle,
                "timeout",
                f"{self.config['control_timeout_seconds']}s",
                "bash",
                "-lc",
                "git add -N -- . && git diff --binary HEAD --",
            ]
        )
        if result.code != 0 or result.timed_out or result.output_limited:
            raise RuntimeError("Cannot export a complete patch")
        return result.output

    def destroy(self, handle: str) -> None:
        self._check(handle)
        result = self._run(["rm", "-f", handle])
        if result.code != 0 or result.timed_out:
            raise RuntimeError("Container cleanup failed; resource requires attention")
        self.owned.remove(handle)
