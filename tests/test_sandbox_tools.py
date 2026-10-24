import json
from pathlib import Path
from typing import Any

import pytest

from ctxroute.domain import Task
from ctxroute.sandbox.docker import DockerSandbox, ProcessResult
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tools.mcp_server import build_server
from ctxroute.tools.service import SandboxService

TASK = Task("fixture", "repo", "commit", "issue")
CONFIG: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "configs/sandbox.json").read_text()
)
IMAGE = "repo/image@sha256:" + "a" * 64


def test_docker_arguments_lifecycle_timeout_and_no_host_mounts() -> None:
    calls: list[list[str]] = []

    def executor(argv: list[str], timeout: float, max_bytes: int) -> ProcessResult:
        calls.append(argv)
        if argv[1] == "exec":
            return ProcessResult("timed out", 124)
        return ProcessResult("created", 0)

    sandbox = DockerSandbox({TASK.instance_id: IMAGE}, CONFIG, executor)
    handle = sandbox.create(TASK)
    create = calls[0]
    for part in [
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "no-new-privileges",
        "--pids-limit",
        "--memory",
        "--cpus",
        "--pull",
        "never",
    ]:
        assert part in create
    assert not {"-v", "--volume", "--mount", "--privileged", "-e"}.intersection(create)
    result = sandbox.execute(handle, "sleep 1000", 0.5)
    assert result.timed_out
    assert calls[-1] == [CONFIG["docker_binary"], "kill", handle]
    assert "0.5s" in calls[-2]
    sandbox.destroy(handle)
    assert calls[-1] == [CONFIG["docker_binary"], "rm", "-f", handle]
    with pytest.raises(ValueError):
        sandbox.destroy("someone-elses-container")


def test_incomplete_patch_cannot_be_exported() -> None:
    def executor(argv: list[str], timeout: float, max_bytes: int) -> ProcessResult:
        return ProcessResult("partial", 0, output_limited=argv[1] == "exec")

    sandbox = DockerSandbox({TASK.instance_id: IMAGE}, CONFIG, executor)
    handle = sandbox.create(TASK)
    with pytest.raises(RuntimeError, match="complete patch"):
        sandbox.patch(handle)
    sandbox.destroy(handle)


def test_require_digest_pinned_image() -> None:
    def forbidden(argv: list[str], timeout: float, max_bytes: int) -> ProcessResult:
        raise AssertionError("Mutable image must fail before invoking Docker")

    sandbox = DockerSandbox({TASK.instance_id: "image:latest"}, CONFIG, forbidden)
    with pytest.raises(ValueError, match="digest-pinned"):
        sandbox.create(TASK)


def test_official_mcp_binding_registers_exact_lifecycle_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ctxroute.tools import mcp_server

    class FakeMcp:
        def __init__(self, name: str) -> None:
            self.names: list[str] = []

        def tool(self) -> Any:
            def register(fn: Any) -> Any:
                self.names.append(fn.__name__)
                return fn

            return register

    def fake_import(name: str) -> SimpleNamespace:
        return SimpleNamespace(FastMCP=FakeMcp)

    monkeypatch.setattr(mcp_server, "import_module", fake_import)
    service = SandboxService(FakeSandbox(), {TASK.instance_id: TASK})
    server = build_server(service)
    assert server.names == ["create_sandbox", "exec", "get_patch", "destroy"]
    handle = service.create_sandbox(TASK.instance_id)
    assert service.exec(handle, "cat x", 1)["exit_code"] == 0
    service.destroy(handle)
