import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from ctxroute.evalkit.build_diagnostics import record_step
from ctxroute.evalkit.harness_source import sanitization_commands, validated_base


def test_failure_persists_both_streams_versions_and_redacts_split_secret(tmp_path: Path) -> None:
    directory = tmp_path / "failed-build"
    fake_secret = "fixture-secret-12345"
    program = (
        "import os,time,sys\n"
        "os.write(1,b'fixture-sec');time.sleep(.02);os.write(1,b'ret-12345\\n')\n"
        "print('Authorization: Bearer fixture-authorization',file=sys.stderr)\n"
        "print('HTTPS_PROXY=https://name:pass@proxy.invalid:3128',file=sys.stderr)\n"
        "print('Fetching https://user:password@packages.invalid/simple/')\n"
        "print('token=fixture-query-token',file=sys.stderr)\n"
        "print('x'*150000)\n"
        "print('Failed building wheel for demo-backend',file=sys.stderr)\n"
        "print('last stdout line');sys.exit(23)\n"
    )
    result = record_step(
        [sys.executable, "-c", program],
        directory,
        step="editable-install",
        package="demo-project",
        python_argv=[sys.executable],
        sensitive_values=[fake_secret],
    )
    assert result["exit_code"] == 23 and result["status"] == "failed"
    assert result["step"] == "editable-install" and result["package"] == "demo-project"
    assert result["failed_packages"] == ["demo-backend"]
    assert result["versions_complete"]
    assert set(result["versions_before"]) == {"pip", "setuptools", "wheel", "python"}
    assert result == json.loads((directory / "result.json").read_text())
    assert (directory / "stdout.log").read_text().endswith("last stdout line\n")
    assert "x" * 150000 in (directory / "stdout.log").read_text()
    logs = "\n".join(p.read_text() for p in directory.rglob("*") if p.is_file())
    for secret in (
        fake_secret,
        "fixture-authorization",
        "proxy.invalid",
        "user:password@",
        "fixture-query-token",
    ):
        assert secret not in logs
    # The persisted result exists even if the caller now raises an assertion.
    with pytest.raises(AssertionError):
        assert result["exit_code"] == 0
    assert (
        (directory / "stderr.log").read_text().endswith("Failed building wheel for demo-backend\n")
    )


def test_timeout_preserves_output_and_closes_process_group(tmp_path: Path) -> None:
    result = record_step(
        [sys.executable, "-c", "import time;print('started',flush=True);time.sleep(30)"],
        tmp_path / "timeout",
        step="build",
        package=None,
        python_argv=[sys.executable],
        timeout=0.2,
    )
    assert result["timed_out"] and result["exit_code"] != 0
    assert (tmp_path / "timeout/stdout.log").read_text() == "started\n"
    assert result["failed_packages"] == []


def test_missing_executable_retains_error_and_cannot_overwrite(tmp_path: Path) -> None:
    directory = tmp_path / "missing"
    result = record_step(
        [str(tmp_path / "missing-program")],
        directory,
        step="install",
        package="fixture",
        python_argv=[sys.executable],
    )
    assert result["exit_code"] is None and result["exception_type"] == "FileNotFoundError"
    assert (directory / "stderr.log").stat().st_size > 0
    with pytest.raises(FileExistsError):
        record_step(
            [sys.executable],
            directory,
            step="install",
            package="fixture",
            python_argv=[sys.executable],
        )


@dataclass
class Spec:
    instance_id: str
    repo: str
    arch: str
    repo_script_list: list[str]


def test_source_spec_uses_metadata_and_reset_without_base_attribute() -> None:
    base = "a" * 40
    metadata = {"instance_id": "fixture", "repo": "example/repo", "base_commit": base}
    spec = Spec("fixture", "example/repo", "arm64", [f"git reset --hard {base}"])
    assert validated_base(spec, metadata, metadata, arch="arm64") == base
    for changed in (replace(spec, repo="other/repo"), replace(spec, repo_script_list=[])):
        with pytest.raises(ValueError):
            validated_base(changed, metadata, metadata, arch="arm64")


@pytest.mark.parametrize("change", [False, True])
def test_setup_commit_cleaning_preserves_base_and_rejects_changed_tree(
    tmp_path: Path,
    change: bool,
) -> None:
    def git(*argv: str) -> str:
        return subprocess.check_output(["git", *argv], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (tmp_path / "source.py").write_text("base\n")
    git("add", "source.py")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "source.py").write_text("future\n")
    git("commit", "-qam", "future")
    future = git("rev-parse", "HEAD")
    git("tag", "future-tag")
    git("reset", "--hard", base)
    if change:
        (tmp_path / "source.py").write_text("unexpected setup change\n")
    git("commit", "--allow-empty", "-qam", "SWE-bench")
    result = subprocess.run(
        ["sh", "-ec", "\n".join(sanitization_commands(base))],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    if change:
        assert result.returncode != 0
        assert (tmp_path / "source.py").read_text() == "unexpected setup change\n"
    else:
        assert result.returncode == 0, result.stderr
        assert git("rev-parse", "HEAD") == base and git("tag") == ""
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", future], cwd=tmp_path, capture_output=True
            ).returncode
            != 0
        )


def test_sanitization_rejects_nonliteral_commit() -> None:
    with pytest.raises(ValueError):
        sanitization_commands("HEAD; true")
