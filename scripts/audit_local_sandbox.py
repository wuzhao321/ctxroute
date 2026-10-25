"""Bounded real-container checks; never call a model or use benchmark grading data."""

import json
import statistics
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import docker

from ctxroute.domain import Task
from ctxroute.sandbox.docker import DockerSandbox
from ctxroute.tools.mcp_client import McpSandbox, StdioClient


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "results/validation/live-preflight"
    out.mkdir(parents=True, exist_ok=True)
    private = root / "results/private"
    private.mkdir(parents=True, exist_ok=True)
    registry_path = root / "configs/private/images.json"
    registry = json.loads(registry_path.read_text())
    tasks_path = root / "data/private/dev-smoke.jsonl"
    task = Task(**json.loads(tasks_path.read_text()))
    config = json.loads((root / "configs/sandbox.json").read_text())
    client = docker.DockerClient(base_url=f"unix://{root}/.tools/vm/ctx/sock/docker.sock")
    sandbox = DockerSandbox(registry, config)
    evidence = {"kind": "real_sandbox_audit_not_benchmark", "checks": {}, "samples_ms": {}}
    checks = evidence["checks"]
    samples = evidence["samples_ms"]
    handles = []
    mcp = None
    mcp_handle = None
    audit_root = tempfile.TemporaryDirectory(dir=private, prefix="mcp-audit-")
    try:
        start = time.monotonic()
        handle = sandbox.create(task)
        handles.append(handle)
        ready = sandbox.execute(handle, "true", 5)
        assert ready.exit_code == 0
        samples["create_to_exec_ready"] = [(time.monotonic() - start) * 1000]
        container = client.containers.get(handle)
        host = container.attrs["HostConfig"]
        checks["network_none"] = host["NetworkMode"] == "none"
        checks["no_mounts"] = container.attrs["Mounts"] == []
        checks["cap_drop_all"] = host["CapDrop"] == ["ALL"]
        checks["no_new_privileges"] = "no-new-privileges" in host["SecurityOpt"]
        checks["resource_limits"] = (
            host["Memory"] == 4 * 1024**3
            and host["NanoCpus"] == 2 * 10**9
            and host["PidsLimit"] == 128
        )
        checks["no_credentials"] = not any(
            key in variable.split("=", 1)[0].upper()
            for variable in container.attrs["Config"].get("Env", [])
            for key in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        )
        command = """python - <<'PY'
import os, socket, sys
assert not os.path.exists('/Users')
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/eval.sh')
assert not os.path.exists('/patch.diff')
assert not os.path.exists('/testbed/.env')
s=socket.socket(); s.settimeout(2)
try:
    s.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise AssertionError('Unexpected network access')
finally:
    s.close()
import sympy
print(sys.executable)
print(sympy.__version__)
PY"""
        observation = sandbox.execute(handle, command, 10)
        checks["filesystem_network_python"] = observation.exit_code == 0
        evidence["environment_probe"] = asdict(observation)
        checks["initial_patch_empty"] = sandbox.patch(handle) == ""
        samples["direct_exec"] = []
        for _ in range(5):
            start = time.monotonic()
            assert sandbox.execute(handle, "true", 5).exit_code == 0
            samples["direct_exec"].append((time.monotonic() - start) * 1000)

        timeout = sandbox.execute(handle, "sleep 20 & wait", 0.5)
        container.reload()
        checks["timeout_stops_container_and_children"] = (
            timeout.timed_out and not container.attrs["State"]["Running"]
        )
        sandbox.destroy(handle)
        handles.remove(handle)
        limited = DockerSandbox(registry, {**config, "output_bytes": 1024})
        limited_handle = limited.create(task)
        try:
            overflow = limited.execute(limited_handle, "python -c 'print(\"x\" * 10000)'", 5)
            checks["output_limit"] = overflow.output_limited and len(overflow.output) <= 1024
            checks["output_limit_stops_container"] = not client.containers.get(
                limited_handle
            ).attrs["State"]["Running"]
        finally:
            limited.destroy(limited_handle)
        # Permit only the sandbox service in a temporary configuration root. The
        # production runtime is unchanged and no model or credential code is loaded.
        audit_configs = Path(audit_root.name) / "configs"
        audit_configs.mkdir()
        (audit_configs / "runtime.json").write_text(json.dumps({"execution_mode": "live"}))
        (audit_configs / "sandbox.json").write_text(
            json.dumps({**config, "docker_binary": str(root / config["docker_binary"])})
        )
        mcp = StdioClient(
            sys.executable,
            [
                "-m",
                "ctxroute.tools.mcp_server",
                "--root",
                audit_root.name,
                "--tasks",
                str(tasks_path),
                "--images",
                str(registry_path),
            ],
        )
        remote = McpSandbox(mcp, config["control_timeout_seconds"])
        start = time.monotonic()
        mcp_handle = remote.create(task)
        assert remote.execute(mcp_handle, "true", 5).exit_code == 0
        samples["mcp_create_to_exec_ready"] = [(time.monotonic() - start) * 1000]
        samples["mcp_exec"] = []
        for _ in range(5):
            obs = remote.execute(mcp_handle, "true", 5)
            assert obs.exit_code == 0
            samples["mcp_exec"].append(obs.rpc_ms)
        checks["mcp_patch_export"] = remote.patch(mcp_handle) == ""
        remote.destroy(mcp_handle)
        mcp_handle = None
        evidence["latency_ms"] = {
            key: {
                "n": len(values),
                "p50": statistics.median(values),
                "p95_nearest_rank": sorted(values)[-1],
            }
            for key, values in samples.items()
        }
        evidence["passed"] = all(checks.values())
    finally:
        if mcp is not None:
            if mcp_handle is not None:
                McpSandbox(mcp, config["control_timeout_seconds"]).destroy(mcp_handle)
            mcp.close()
        for handle in handles:
            sandbox.destroy(handle)
        client.close()
        audit_root.cleanup()
        (out / "sandbox-audit.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if not evidence.get("passed"):
        raise RuntimeError("Sandbox audit did not pass; paid inference remains disabled")


if __name__ == "__main__":
    main()
