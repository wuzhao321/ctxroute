"""Pin the native baseline before loading its optional dependencies."""

import hashlib
import json
import os
from dataclasses import dataclass
from importlib import import_module, metadata, util
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NativeAPI:
    agent: Any
    actions: Any
    exceptions: Any
    docker_environment: Any
    config: dict[str, Any]
    policy: dict[str, Any]


def load_native(root: Path) -> NativeAPI:
    """No installation/download fallback. Called only after the live-execution gate."""
    policy = json.loads((root / "configs/baselines/mini-swe-agent.json").read_text())
    try:
        version = metadata.version("mini-swe-agent")
    except metadata.PackageNotFoundError:
        raise RuntimeError(
            "mini-swe-agent is not installed; environment setup remains deferred"
        ) from None
    if version != policy["upstream_version"]:
        raise RuntimeError("Installed mini-swe-agent version differs from the frozen baseline")
    spec = util.find_spec("minisweagent")
    if spec is None or spec.origin is None:
        raise RuntimeError("Cannot locate installed mini-swe-agent source")
    package = Path(spec.origin).resolve().parent
    for relative, expected in policy["file_git_blobs"].items():
        path = (package / relative).resolve()
        if not path.is_relative_to(package):
            raise ValueError("Unsafe upstream source path")
        content = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Pinned upstream source differs: {relative}")
    # Upstream imports read a global .env. Keep that configuration inside this project,
    # and reject an already-loaded package pointing to another user's/global directory.
    config_home = root / ".cache/minisweagent"
    config_home.mkdir(parents=True, exist_ok=True)
    if (config_home / ".env").exists():
        raise RuntimeError("Baseline private config directory must not contain a .env file")
    environment = {"MSWEA_GLOBAL_CONFIG_DIR": str(config_home), "MSWEA_SILENT_STARTUP": "1"}
    previous = {key: os.environ.get(key) for key in environment}
    try:
        os.environ.update(environment)
        module = import_module("minisweagent")
        if Path(module.global_config_dir).resolve() != config_home.resolve():
            raise RuntimeError(
                "mini-swe-agent was already imported with a different config directory"
            )
        native_config = import_module("yaml").safe_load(
            (package / policy["config_file"]).read_text()
        )
        return NativeAPI(
            import_module("minisweagent.agents.default").DefaultAgent,
            import_module("minisweagent.models.utils.actions_text"),
            import_module("minisweagent.exceptions"),
            import_module("minisweagent.environments.docker").DockerEnvironment,
            native_config,
            policy,
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
