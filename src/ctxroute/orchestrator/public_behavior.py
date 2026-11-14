"""Versioned target scaffold derived exclusively from the public issue's CLI behavior.

This fixture is not a reference implementation or an official grading test.
Names of internal modules, implementation branches and diagnostic ratings are
not part of the expected behavior. Sentinel diagnostics prove files are checked.
"""

import hashlib
import json

from ctxroute.domain import Task

TARGET_PATH = "tests/test_ctxroute_public_behavior.py"
STAGED_PATH = "/tmp/ctxroute_public_behavior.py"
PUBLIC_TARGET = '''"""Public same-name directory behavior, import side case and file controls."""
import json
import sys
import subprocess
import pytest

def run_pylint(root, *args):
    proc = subprocess.run(
        [sys.executable, "-m", "pylint", "--output-format=json", "--score=n", *args],
        cwd=root, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode < 32, (proc.returncode, proc.stdout, proc.stderr)
    return proc.returncode, json.loads(proc.stdout)

def files(root, sentinel=False):
    folder = root / "a"
    folder.mkdir()
    for name in ("a", "b"):
        (folder / (name + ".py")).write_text("print(MISSING_VALUE)\\n" if sentinel else "")
    (root / "r.py").write_text("from a import b\\n")

def test_public_empty_directory(tmp_path):
    """pylint a must succeed without requiring a/__init__.py."""
    files(tmp_path)
    code, messages = run_pylint(tmp_path, "a")
    assert code == 0, messages
    assert messages == []

def test_public_import_side_case(tmp_path):
    """Adding a to pylint -E r must not break the public from-a-import-b example."""
    files(tmp_path)
    control_code, control_messages = run_pylint(tmp_path, "-E", "r")
    assert control_code == 0, control_messages
    code, messages = run_pylint(tmp_path, "-E", "r", "a")
    assert code == 0, messages

@pytest.mark.parametrize("file", ["a/a.py", "a/b.py"])
def test_public_each_file_is_checked(tmp_path, file):
    """Explicit individual files must produce the deliberate undefined-variable diagnostic."""
    files(tmp_path, sentinel=True)
    code, messages = run_pylint(tmp_path, "--disable=all", "--enable=undefined-variable", file)
    assert code == 2, messages
    assert any(m["symbol"] == "undefined-variable" and
               (tmp_path / m["path"]).resolve() == (tmp_path / file).resolve() for m in messages)

def test_public_directory_checks_both_files(tmp_path):
    """A directory must actually analyze both files, not merely suppress the parse error."""
    files(tmp_path, sentinel=True)
    code, messages = run_pylint(tmp_path, "--disable=all", "--enable=undefined-variable", "a")
    assert code == 2, messages
    observed = {(tmp_path / m["path"]).resolve() for m in messages
                if m["symbol"] == "undefined-variable"}
    assert {(tmp_path / "a/a.py").resolve(), (tmp_path / "a/b.py").resolve()} <= observed
'''


def contract_sha() -> str:
    return hashlib.sha256(PUBLIC_TARGET.encode()).hexdigest()


def stage_command(task: Task) -> str:
    if task.instance_id != "pylint-dev__pylint-7114":
        raise ValueError("No preregistered public behavior scaffold for this task")
    return (
        "python - <<'CTXROUTE_PUBLIC_BEHAVIOR'\nfrom pathlib import Path\n"
        + f"Path({STAGED_PATH!r}).write_text({json.dumps(PUBLIC_TARGET)})\n"
        + "CTXROUTE_PUBLIC_BEHAVIOR"
    )
