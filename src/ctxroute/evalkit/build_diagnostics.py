"""Durable, credential-redacted subprocess evidence for environment preparation.

This module is also usable as a standalone file inside an isolated build container.
It never loads application credentials or benchmark data.
"""

import codecs
import json
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

VERSION_PROBE = """import importlib.metadata,json,platform
versions={'python':platform.python_version()}
for name in ('pip','setuptools','wheel'):
    try: versions[name]=importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError: versions[name]=None
print(json.dumps(versions))
"""


def redact(text: str, sensitive_values: Sequence[str] = ()) -> str:
    """Retain build errors while removing credentials and complete proxy endpoints."""
    text = re.sub(
        r"(?i)(\b(?:https?|all)_proxy\b[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[REDACTED]",
        text,
    )
    for value in sorted((v for v in sensitive_values if v), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(
        r"(?im)\b(authorization|proxy-authorization)\s*:[^\r\n]*", r"\1: [REDACTED]", text
    )
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED]", text)
    text = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[REDACTED]@", text)
    text = re.sub(
        r"(?i)(\b(?:[a-z0-9_]*api[_-]?key|[a-z0-9_]*token|[a-z0-9_]*password|"
        r"[a-z0-9_]*secret|(?:https?|all)_proxy)\b[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,&}\]]+)",
        r"\1[REDACTED]",
        text,
    )
    return text


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _capture(
    argv: Sequence[str],
    directory: Path,
    *,
    timeout: float,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    sensitive_values: Sequence[str],
) -> dict[str, Any]:
    """Flush redacted complete lines as they arrive; drain both pipes on timeout."""
    started = time.monotonic()
    result: dict[str, Any] = {"exit_code": None, "timed_out": False, "exception_type": None}
    streams: dict[int, TextIO] = {}
    buffers: dict[int, str] = {}
    decoders: dict[int, Any] = {}
    proc: subprocess.Popen[bytes] | None = None
    with (
        (directory / "stdout.log").open("x") as stdout,
        (directory / "stderr.log").open("x") as stderr,
        selectors.DefaultSelector() as selector,
    ):
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert proc.stdout is not None and proc.stderr is not None
            for pipe, output in ((proc.stdout, stdout), (proc.stderr, stderr)):
                fd = pipe.fileno()
                streams[fd], buffers[fd] = output, ""
                decoders[fd] = codecs.getincrementaldecoder("utf-8")(errors="replace")
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() - started >= timeout and not result["timed_out"]:
                    result["timed_out"] = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for key, _ in selector.select(0.1):
                    fd = key.fd
                    chunk = os.read(fd, 65536)
                    buffers[fd] += decoders[fd].decode(chunk, final=not chunk)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        streams[fd].write(redact(buffers.pop(fd), sensitive_values))
                    else:
                        # Keep the unfinished line so a split credential cannot escape redaction.
                        boundary = buffers[fd].rfind("\n") + 1
                        streams[fd].write(redact(buffers[fd][:boundary], sensitive_values))
                        buffers[fd] = buffers[fd][boundary:]
                    streams[fd].flush()
            result["exit_code"] = proc.wait()
        except Exception as exc:
            result["exception_type"] = type(exc).__name__
            stderr.write(redact(str(exc), sensitive_values) + "\n")
        finally:
            if proc is not None:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                result["exit_code"] = proc.returncode
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            for output in (stdout, stderr):
                output.flush()
                os.fsync(output.fileno())
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return result


def record_step(
    argv: Sequence[str],
    directory: Path,
    *,
    step: str,
    package: str | None,
    python_argv: Sequence[str],
    timeout: float = 300,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    sensitive_values: Sequence[str] = (),
) -> dict[str, Any]:
    """Persist versions, output and status before the caller checks success.

    ``package`` identifies the package being operated on, not a guessed root cause.
    Version probes must address the same interpreter/environment as the build.
    """
    if not argv or not python_argv or timeout <= 0 or not step:
        raise ValueError("Command, interpreter, step and positive timeout are required")
    directory.mkdir(parents=True, exist_ok=False)
    record: dict[str, Any] = {
        "schema": 1,
        "step": step,
        "package": package,
        "argv": [redact(arg, sensitive_values) for arg in argv],
        "started_at": datetime.now(UTC).isoformat(),
        "status": "started",
        "stdout": "stdout.log",
        "stderr": "stderr.log",
        "output_truncated": False,
    }
    _write_json(directory / "result.json", record)
    for phase in ("before", "after"):
        probe_dir = directory / f"versions-{phase}"
        probe_dir.mkdir()
        probe = _capture(
            [*python_argv, "-c", VERSION_PROBE],
            probe_dir,
            timeout=30,
            cwd=cwd,
            env=env,
            sensitive_values=sensitive_values,
        )
        versions: dict[str, Any] = dict.fromkeys(("python", "pip", "setuptools", "wheel"))
        if probe["exit_code"] == 0:
            try:
                parsed = json.loads((probe_dir / "stdout.log").read_text())
                if isinstance(parsed, dict):
                    values = cast(dict[str, Any], parsed)
                    versions = {key: values.get(key) for key in versions}
                    if not isinstance(versions["python"], str):
                        probe["parse_error"] = True
                else:
                    probe["parse_error"] = True
            except (ValueError, OSError):
                probe["parse_error"] = True
        record[f"versions_{phase}"] = versions
        record[f"version_probe_{phase}"] = probe
        _write_json(directory / "result.json", record)
        if phase == "before":
            record.update(
                _capture(
                    argv,
                    directory,
                    timeout=timeout,
                    cwd=cwd,
                    env=env,
                    sensitive_values=sensitive_values,
                )
            )
            record["status"] = "command_finished"
            _write_json(directory / "result.json", record)
    record["status"] = (
        "passed"
        if (record["exit_code"] == 0 and not record["timed_out"] and not record["exception_type"])
        else "failed"
    )
    record["finished_at"] = datetime.now(UTC).isoformat()
    output = (directory / "stdout.log").read_text() + (directory / "stderr.log").read_text()
    record["failed_packages"] = sorted(
        set(
            re.findall(
                r"(?im)(?:Failed building wheel for|Could not build wheels for|"
                r"No matching distribution found for)\s+([A-Za-z0-9_.-]+)",
                output,
            )
        )
    )
    record["failure_package_identification"] = (
        "explicit_error_output" if record["failed_packages"] else "not_identified"
    )
    record["versions_complete"] = all(
        record[f"version_probe_{phase}"]["exit_code"] == 0
        and not record[f"version_probe_{phase}"].get("parse_error")
        for phase in ("before", "after")
    )
    _write_json(directory / "result.json", record)
    return record
