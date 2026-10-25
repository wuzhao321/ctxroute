"""Opt-in sandbox timing; never invoked by checks or inference automatically."""

import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from ctxroute.domain import Task
from ctxroute.evalkit.statistics import percentile
from ctxroute.sandbox.base import Sandbox


def compare_transports(
    task: Task,
    direct: Callable[[], Sandbox],
    mcp: Callable[[], Sandbox],
    *,
    repetitions: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        raise RuntimeError("Sandbox timing requires execute=True")
    if not 1 <= repetitions <= 100:
        raise ValueError("Repetitions must be between 1 and 100")
    rows: list[dict[str, Any]] = []
    for index in range(repetitions):
        # Alternate order to make first-run/cache effects visible.
        order = [("direct", direct), ("mcp", mcp)]
        if index % 2:
            order.reverse()
        for transport, factory in order:
            sandbox = factory()
            handle = None
            start = time.monotonic()
            try:
                handle = sandbox.create(task)
                created = time.monotonic()
                result = sandbox.execute(handle, "pwd", 10)
                end = time.monotonic()
                rows.append(
                    {
                        "repetition": index,
                        "transport": transport,
                        "create_ms": (created - start) * 1000,
                        "first_exec_ready_ms": (end - start) * 1000,
                        "exec_roundtrip_ms": (end - created) * 1000,
                        "observation": asdict(result),
                    }
                )
            finally:
                if handle is not None:
                    sandbox.destroy(handle)
    summaries: dict[str, Any] = {}
    for transport in ("direct", "mcp"):
        values = [row["exec_roundtrip_ms"] for row in rows if row["transport"] == transport]
        summaries[transport] = {
            "n": len(values),
            "p50_ms": percentile(values, 0.5),
            "p95_ms": percentile(values, 0.95),
        }
    return {
        "rows": rows,
        "summaries": summaries,
        "note": (
            "RPC roundtrip includes execution; direct/MCP differences include container variance."
        ),
    }
