"""Stable provenance for experiment code and controlled variables."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ctxroute.config import ArmConfig
from ctxroute.orchestrator.engine import SUMMARY_PROMPT, action_prompt


def code_fingerprint(root: Path) -> str:
    entries = [
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted((root / "src/ctxroute").rglob("*.py"))
    ]
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def controlled_variables(
    cfg: ArmConfig,
    runtime: dict[str, Any],
    sandbox: dict[str, Any],
    *,
    native_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if native_config is None:
        prompts = {"action": action_prompt(cfg), "compression": SUMMARY_PROMPT}
        protocol = "json_action_v2"
    else:
        prompts = {"agent": native_config["agent"], "model": native_config["model"]}
        protocol = "native_text_action"
    return {
        "action_protocol": protocol,
        "response_format": "json_object" if native_config is None else "text",
        "prompt_config_sha256": hashlib.sha256(
            json.dumps(prompts, sort_keys=True).encode()
        ).hexdigest(),
        "context": asdict(cfg.context),
        "router": asdict(cfg.router),
        "limits": asdict(cfg.limits),
        "temperature": 0,
        "thinking": runtime["thinking"],
        "model_transport": "http" if native_config is not None else runtime["model_transport"],
        "tool_transport": runtime["tool_transport"],
        "sandbox": sandbox,
        "format_difference_from_A1": native_config is not None,
    }
