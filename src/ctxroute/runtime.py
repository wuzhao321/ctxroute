"""Composition roots. Offline demo is independent of credentials and Docker."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ctxroute.api_probe import http_transport, source_commit
from ctxroute.baselines.mini import MiniSweRunner
from ctxroute.baselines.upstream import load_native
from ctxroute.config import ArmConfig
from ctxroute.domain import Task
from ctxroute.evalkit.provenance import code_fingerprint, controlled_variables
from ctxroute.models.deepseek import DeepSeekModel
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.base import Runner
from ctxroute.orchestrator.engine import Engine, RunResult
from ctxroute.sandbox.base import Sandbox
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.secrets import load_key
from ctxroute.tracing.budget import Ledger
from ctxroute.tracing.cost import money
from ctxroute.tracing.writer import RunWriter


def demo(root: Path, arm: str, output: Path) -> RunResult:
    config = ArmConfig.load(root / f"configs/arms/{arm}.yaml")
    writer = RunWriter(
        output,
        "synthetic-" + uuid4().hex[:12],
        {
            "kind": "synthetic_demo",
            "is_benchmark": False,
            "arm": arm,
            "config": asdict(config),
            "harness_version": None,
            "image_digest": None,
        },
    )
    model = ScriptedModel(
        [
            '{"tool":"bash","command":"cat example.py"}',
            '{"tool":"bash","command":"python -m pytest"}',
            '{"tool":"submit"}',
        ]
    )
    sandbox = FakeSandbox(
        patch=(
            "diff --git a/example.py b/example.py\n--- a/example.py\n"
            "+++ b/example.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
    )
    task = Task(
        "synthetic-demo", "example/repo", "fixture", "Synthetic example only: fix example.py"
    )
    return Engine(config, model, sandbox, writer).run(task, seed=0)


def build_live_engine(
    root: Path,
    arm: str,
    task: Task,
    sandbox: Sandbox,
    *,
    run_id: str | None = None,
    config_path: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> Runner:
    runtime: dict[str, Any] = json.loads((root / "configs/runtime.json").read_text())
    budget: dict[str, Any] = json.loads((root / "configs/budget.json").read_text())
    if runtime["execution_mode"] != "live" or budget["paid_execution_enabled"] is not True:
        raise RuntimeError("Live execution is disabled; current scope is code-only")
    cfg_path = config_path or root / f"configs/arms/{arm}.yaml"
    cfg = ArmConfig.load(cfg_path)
    if cfg.arm != arm or cfg.engine not in {"ctxroute", "mini-swe-agent"}:
        raise ValueError("Unsupported or mismatched experiment engine")
    native = load_native(root) if cfg.engine == "mini-swe-agent" else None
    sandbox_cfg = json.loads((root / "configs/sandbox.json").read_text())
    transport_name = "http" if native is not None else runtime.get("model_transport", "http")
    if transport_name == "pydantic_ai":
        from ctxroute.models.pydantic_adapter import pydantic_transport

        transport = pydantic_transport
    elif transport_name == "http":
        transport = http_transport
    else:
        raise ValueError("Unknown model transport")
    price_path = root / runtime["price_file"]
    prices = json.loads(price_path.read_text())
    image_digest = (metadata or {}).get("image_digest") or runtime["image_digest"]
    if not image_digest or not runtime["harness_version"]:
        raise RuntimeError("Live execution requires verified image and harness metadata")
    if prices["retrieved_on"] != datetime.now(UTC).date().isoformat():
        raise RuntimeError("Price snapshot requires same-day verification before paid execution")
    key = load_key(root)
    run_id = run_id or f"{arm}-" + uuid4().hex
    writer = RunWriter(
        root / "results/runs",
        run_id,
        {
            **(metadata or {}),
            "kind": "agent_run",
            "instance_id": task.instance_id,
            "arm": arm,
            "commit": source_commit(root),
            "config_sha256": hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
            "price_sha256": hashlib.sha256(price_path.read_bytes()).hexdigest(),
            "model_ids": {tier: entry["model_id"] for tier, entry in prices["models"].items()},
            "harness_version": runtime["harness_version"],
            "image_digest": image_digest,
            "engine": cfg.engine,
            "model_transport": transport_name,
            "source_tree_sha256": code_fingerprint(root),
            "controls": controlled_variables(
                cfg,
                runtime,
                sandbox_cfg,
                native_config=native.config if native is not None else None,
            ),
            **(
                {
                    "upstream_commit": native.policy["upstream_commit"],
                    "upstream_version": native.policy["upstream_version"],
                    "native_profile": native.policy["profile"],
                    "native_source_blobs": native.policy["file_git_blobs"],
                    "baseline_policy_sha256": hashlib.sha256(
                        (root / "configs/baselines/mini-swe-agent.json").read_bytes()
                    ).hexdigest(),
                    "native_prompt_differs_from_A1": True,
                }
                if native is not None
                else {}
            ),
        },
    )
    writer.snapshot(
        "config", {**asdict(cfg), **({"native": native.config} if native is not None else {})}
    )
    writer.snapshot("task", asdict(task))
    ledger = Ledger(
        root / "results/budget/campaign.sqlite",
        hard_limit=money(str(budget["hard_limit"])),
        pause_at=money(str(budget["pause_at"])),
        enabled=True,
    )
    model = DeepSeekModel(
        key=key,
        prices=prices,
        ledger=ledger,
        writer=writer,
        input_guard_tokens=runtime["input_guard_tokens"],
        thinking=runtime["thinking"],
        transport=transport,
    )
    if native is not None:
        return MiniSweRunner(
            cfg,
            model,
            sandbox,
            writer,
            native,
            cwd=sandbox_cfg["workdir"],
            model_attempts=runtime["model_attempts"],
            backoff_seconds=runtime["backoff_seconds"],
        )
    return Engine(
        cfg,
        model,
        sandbox,
        writer,
        model_attempts=runtime["model_attempts"],
        backoff_seconds=runtime["backoff_seconds"],
    )
