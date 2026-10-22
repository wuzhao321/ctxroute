"""Persistent stdio MCP session in one async task, exposed to the synchronous loop."""

import asyncio
import json
import os
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, replace
from importlib import import_module
from typing import Any, cast

from ctxroute.domain import Observation, Task


@dataclass
class Call:
    tool: str
    arguments: dict[str, Any]
    future: Future[Any]


class StdioClient:
    def __init__(self, command: str, args: list[str], startup_timeout: float = 15) -> None:
        self.command, self.args = command, args
        self.queue: queue.Queue[Call | None] = queue.Queue()
        self.ready: Future[None] = Future()
        self.closed = False
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
        self.ready.result(timeout=startup_timeout)

    def _worker(self) -> None:
        try:
            asyncio.run(self._session())
        except Exception:
            error = RuntimeError("MCP session unavailable or disconnected")
            if not self.ready.done():
                self.ready.set_exception(error)
            while True:
                try:
                    item = self.queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None and not item.future.done():
                    item.future.set_exception(error)
        finally:
            self.closed = True

    async def _session(self) -> None:
        sdk = import_module("mcp")
        stdio = import_module("mcp.client.stdio")
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "SYSTEMROOT", "USERPROFILE")
            if key in os.environ
        }
        params = sdk.StdioServerParameters(command=self.command, args=self.args, env=env)
        async with stdio.stdio_client(params) as (read, write):
            async with sdk.ClientSession(read, write) as session:
                await session.initialize()
                self.ready.set_result(None)
                while True:
                    call = await asyncio.to_thread(self.queue.get)
                    if call is None:
                        return
                    try:
                        result = await session.call_tool(call.tool, call.arguments)
                        if result.isError:
                            raise RuntimeError("MCP tool returned an error")
                        structured = getattr(result, "structuredContent", None)
                        if structured is not None:
                            value = structured
                            if isinstance(value, dict) and set(cast(dict[str, Any], value)) == {
                                "result"
                            }:
                                value = cast(dict[str, Any], value)["result"]
                        else:
                            text = "".join(
                                block.text for block in result.content if block.type == "text"
                            )
                            try:
                                value = json.loads(text)
                            except json.JSONDecodeError:
                                value = text
                        call.future.set_result(value)
                    except Exception:
                        call.future.set_exception(RuntimeError("MCP tool call failed"))

    def call(self, tool: str, arguments: dict[str, Any], timeout: float) -> Any:
        if self.closed or tool not in {"create_sandbox", "exec", "get_patch", "destroy"}:
            raise RuntimeError("Closed session or unknown tool")
        future: Future[Any] = Future()
        self.queue.put(Call(tool, arguments, future))
        return future.result(timeout=timeout)

    def close(self) -> None:
        self.queue.put(None)
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError("MCP shutdown incomplete")
        self.closed = True


class McpSandbox:
    def __init__(self, client: StdioClient, control_timeout: float) -> None:
        self.client, self.control_timeout = client, control_timeout

    def create(self, task: Task) -> str:
        value = self.client.call(
            "create_sandbox", {"instance_id": task.instance_id}, self.control_timeout
        )
        if not isinstance(value, str):
            raise ValueError("Invalid MCP handle")
        return value

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        start = time.monotonic()
        raw = self.client.call(
            "exec",
            {"handle": handle, "cmd": command, "timeout": timeout},
            timeout + self.control_timeout,
        )
        if not isinstance(raw, dict):
            raise ValueError("Invalid MCP observation")
        observation = Observation(**cast(dict[str, Any], raw))
        return replace(observation, rpc_ms=(time.monotonic() - start) * 1000)

    def patch(self, handle: str) -> str:
        value = self.client.call("get_patch", {"handle": handle}, self.control_timeout)
        if not isinstance(value, str):
            raise ValueError("Invalid MCP patch")
        return value

    def destroy(self, handle: str) -> None:
        self.client.call("destroy", {"handle": handle}, self.control_timeout)
