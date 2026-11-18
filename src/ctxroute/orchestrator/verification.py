"""Conservative test evidence, independent of routing and command execution.

Scopes describe invocations, not proven behavioral coverage. Opaque shell programs
can supply failure evidence but cannot earn a pass from their outer exit alone.
"""

import hashlib
import re
import shlex
from dataclasses import asdict, dataclass
from typing import Any

from ctxroute.domain import Observation
from ctxroute.router.rules import classify


@dataclass(frozen=True)
class CheckEvidence:
    command: str
    scope: str
    targets: tuple[str, ...]
    runner: str
    options: tuple[str, ...]
    outcome: str
    exit_code: int
    test_exit_code: int | None
    attribution: str
    patch_sha256_before: str
    patch_sha256_after: str
    step: int
    diagnostic: str = ""

    def record(self) -> dict[str, Any]:
        record = asdict(self)
        if not self.diagnostic:
            record.pop("diagnostic")
        return record

    def unexecuted_guidance(self) -> tuple[str, str]:
        detail = f"Step {self.step}, {self.command!r}: {self.diagnostic} "
        if self.attribution == "runner_unavailable":
            return "restore_test_environment", detail + (
                "Keep the current diff. Check the repository's documented test runner and run "
                "the same target if possible. Record that result separately. Rerun the recorded "
                "command when its runner is available; otherwise report this verification blocker."
            )
        return "verify_patch", detail + (
            "Check the reported import, selection or configuration and run the intended tests "
            "on this diff."
        )

    def covers(self, other: "CheckEvidence") -> bool:
        if self.scope == other.scope:
            return True
        return (
            self.runner == other.runner == "pytest"
            and self.options == other.options
            and bool(self.targets)
            and bool(other.targets)
            and set(other.targets) <= set(self.targets)
        )


def _invocation(command: str) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    if any(c in command for c in ";|><`$\n&\\"):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if words[:2] in (["python", "-m"], ["python3", "-m"]):
        words = words[2:]
    if not words or words[0] not in {"pytest", "unittest", "tox", "pylint"}:
        return None
    runner, args = words[0], words[1:]
    if any(arg in {"--collect-only", "--co", "--help", "-h", "--version", "-V"} for arg in args):
        return None
    # Only simple explicit pytest selectors admit subset comparison. Options that
    # alter selection/configuration keep an exact invocation scope instead.
    selectors = tuple(arg for arg in args if arg not in {"-q", "-v", "-vv"})
    if runner == "pytest" and all(not arg.startswith("-") for arg in selectors):
        return runner, selectors, ()
    return runner, (), tuple(args)


def _unexecuted(runner: str, obs: Observation, direct_exit: bool) -> tuple[str, str] | None:
    """Match runner diagnostics without attributing a shell prefix's exit to pytest."""
    output, code = obs.output, obs.exit_code
    if re.search(r"\b[1-9]\d* (?:passed|failed|xpassed|xfailed)\b", output):
        return None
    if code:
        missing = re.search(
            rf"(?m)\b(?:No module named ['\"]?{runner}['\"]?|{runner}: (?:command )?not found)$",
            output.rstrip(),
        )
        if missing:
            return "runner_unavailable", missing.group().strip()[:400]
    if runner == "pytest":
        if code == 2 and "ERROR collecting" in output:
            return "collection_error", "Test collection stopped before execution."
        if code and "ImportError while loading conftest" in output:
            return "test_import_error", "pytest could not import the repository's conftest."
        missing_import = re.search(r"(?m)^ModuleNotFoundError: No module named '[\w.]+'$", output)
        if code and missing_import and not re.search(r"\b[1-9]\d* errors?\b", output):
            return "test_import_error", missing_import.group()[:400]
        if direct_exit and code == 4:
            return "invalid_test_invocation", "pytest reported a usage/configuration error."
        if direct_exit and code == 5:
            return "no_tests_executed", "pytest collected no selected tests."
        if code == 0 and re.search(r"(?m)^=*\s*[1-9]\d* skipped\b", output):
            return "no_tests_executed", "pytest reported skips without any passing tests."
    if re.search(r"\bno tests ran\b|\bRan 0 tests\b", output):
        return "no_tests_executed", "The runner reported zero executed tests."
    return None


def observe_check(
    command: str, observation: Observation, before: str, after: str, step: int
) -> CheckEvidence | None:
    if observation.timed_out or observation.output_limited:
        return None
    invocation = _invocation(command)
    scope_command = command
    attribution = "direct_exit"
    if invocation is None and "&&" in command:
        parts = [part.strip() for part in command.split("&&")]
        # A successful read prefix followed by a final test is common. A failing
        # prefix is not attributed to the test unless framework output exists.
        prefixes = parts[:-1]
        if all(
            classify(part) == "read"
            or re.fullmatch(r"git diff(?: --)?(?: [a-zA-Z0-9_./-]+)*", part)
            or re.fullmatch(r"cd (?:-- )?(?:/|\.)[a-zA-Z0-9_./-]*", part)
            for part in prefixes
        ):
            invocation = _invocation(parts[-1])
            # Retain cd in the scope: identical relative paths in other working
            # directories are not the same tests.
            scope_command = " && ".join([p for p in prefixes if p.startswith("cd ")] + parts[-1:])
            attribution = "final_test_after_read_prefix"
    output = observation.output
    summary = bool(
        re.search(
            r"\b\d+ (?:passed|failed|errors?)\b|\b(?:FAILED|ERROR) (?:tests?/|collecting)", output
        )
    )
    diagnostic_failure = bool(
        re.search(
            r"\b[1-9]\d* (?:failed|errors?)\b|\b(?:FAILED|ERROR) (?:tests?/|collecting)", output
        )
        or re.search(r"(?m)^.+:\d+:\d+: [EF]\d{4}:", output)
    )
    direct = invocation is not None
    not_started = None
    if not direct:
        # Do not parse arbitrary shell or Python. Record a conservative scoped
        # failure when a check-like command and its diagnostic agree.
        if (
            classify(command) == "read"
            or not diagnostic_failure
            or not re.search(r"\b(?:pytest|unittest|pylint)\b", command)
        ):
            return None
        runner, targets, options = "opaque_check", (), ()
        outcome, attribution = "failed", "diagnostic_in_compound_command"
    else:
        assert invocation is not None
        runner, targets, options = invocation
        not_started = _unexecuted(runner, observation, attribution == "direct_exit")
        if (
            not not_started
            and attribution != "direct_exit"
            and observation.exit_code
            and not summary
            and not diagnostic_failure
        ):
            return None
        outcome = "failed" if observation.exit_code or diagnostic_failure else "passed"
        if not_started is not None:
            outcome, attribution = "not_run", not_started[0]
    scope = hashlib.sha256(scope_command.strip().encode()).hexdigest()
    if direct and not scope_command.startswith("cd "):
        scope = hashlib.sha256(repr((runner, targets, options)).encode()).hexdigest()
    elif scope_command.startswith("cd "):
        # No selector subset inference across working directories.
        targets, options = (), (scope_command,)
    return CheckEvidence(
        command,
        scope,
        targets,
        runner,
        options,
        outcome,
        observation.exit_code,
        observation.exit_code
        if direct and not not_started and not (diagnostic_failure and not observation.exit_code)
        else None,
        attribution,
        hashlib.sha256(before.encode()).hexdigest(),
        hashlib.sha256(after.encode()).hexdigest(),
        step,
        not_started[1] if not_started else "",
    )
