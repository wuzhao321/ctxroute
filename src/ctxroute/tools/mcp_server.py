"""Optional official MCP SDK adapter; importing the core never requires the SDK."""

from importlib import import_module
from typing import Any

from ctxroute.tools.service import SandboxService


def build_server(service: SandboxService) -> Any:
    try:
        fast_mcp = import_module("mcp.server.fastmcp").FastMCP
    except ImportError:
        raise RuntimeError(
            "MCP SDK is not installed; environment setup is currently deferred"
        ) from None
    server = fast_mcp("ctxroute-sandbox-exec")
    server.tool()(service.create_sandbox)
    server.tool()(service.exec)
    server.tool()(service.get_patch)
    server.tool()(service.destroy)
    return server


def serve(service: SandboxService) -> None:
    build_server(service).run(transport="stdio")


def main() -> None:
    import argparse
    import json
    from pathlib import Path

    from ctxroute.evalkit.campaign import load_tasks
    from ctxroute.sandbox.docker import DockerSandbox

    parser = argparse.ArgumentParser(description="ctxroute isolated sandbox stdio service")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    args = parser.parse_args()
    runtime = json.loads((args.root / "configs/runtime.json").read_text())
    if runtime["execution_mode"] != "live":
        raise RuntimeError("Sandbox execution is disabled during code-only development")
    tasks = load_tasks(args.tasks)
    registry = json.loads(args.images.read_text())
    config = json.loads((args.root / "configs/sandbox.json").read_text())
    sandbox = DockerSandbox(registry, config)
    try:
        serve(SandboxService(sandbox, tasks))
    finally:
        for handle in list(sandbox.owned):
            sandbox.destroy(handle)


if __name__ == "__main__":
    main()
