"""Target red/green evidence and strict single-action execution regressions."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_repair_policy import BAD, GOOD, ORIGINAL, PublicSandbox

from ctxroute.config import ArmConfig, ProgressConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.manager import ContextManager
from ctxroute.domain import Action, Observation, Step, Task
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine, action_prompt
from ctxroute.orchestrator.public_tests import select_tests
from ctxroute.orchestrator.repair_policy import RepairPolicy
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).parents[1]
BASE = {
    **ORIGINAL,
    "core.py": (
        "def normalize(value):\n    return 'UNHANDLED' if value is None else value.strip()\n"
    ),
}
TARGET = (
    "from core import normalize\ndef test_expected():\n"
    "    '''Handle None while retaining actual normalized strings.'''\n"
    "    assert normalize(None) == ''\n    assert normalize(' x ') == 'x'\n"
)


def config() -> ArmConfig:
    cfg = ArmConfig.load(ROOT / "configs/candidates/A1-checkpoint-16k.json")
    return replace(
        cfg, progress=replace(cfg.progress, regression_strategy=True, target_action_strategy=True)
    )


def selection(source: str | None = None, target: str = TARGET) -> str:
    current = {"tests/test_target.py": target}
    if source is not None:
        current["core.py"] = source
    return json.dumps(select_tests(BASE, current, list(current), target_contract=True))


def test_missing_baseline_assertion_or_changed_target_cannot_complete() -> None:
    p = RepairPolicy(config().progress)
    p.install_plan(selection(GOOD), "patch")
    while p.queue:
        j = p.queue.pop(0)
        p.observe(j.command, Observation("1 passed", 0), "patch", "patch", 1, job=j)
    assert not p.can_submit
    assert p.status(100, 20)["next_priority"] == "establish_target_assertion"
    p.install_plan(selection(), "test-only")
    j = p.queue.pop(0)
    p.observe(
        j.command, Observation("E AssertionError\n1 failed", 1), "test-only", "test-only", 2, job=j
    )
    assert p.target_contract and p.target_contract.red_proven
    p.install_plan(selection(GOOD, TARGET.replace("== 'x'", "!= 'wrong'")), "weakened")
    assert not p.target_contract.red_proven and not p.can_submit


@pytest.mark.parametrize(
    "output,code",
    [
        ("1 passed", 0),
        ("1 skipped", 0),
        ("ImportError\n1 error", 2),
        ("AssertionError\n1 failed, 1 error", 1),
    ],
)
def test_baseline_requires_an_executed_assertion_failure(output: str, code: int) -> None:
    p = RepairPolicy(config().progress)
    p.install_plan(selection(), "test-only")
    j = p.queue.pop(0)
    p.observe(j.command, Observation(output, code), "test-only", "test-only", 1, job=j)
    assert p.target_contract and not p.target_contract.red_proven


def test_print_only_and_constant_assertions_do_not_establish_target() -> None:
    p = RepairPolicy(config().progress)
    p.install_plan(
        selection(target="def test_empty():\n    print('ok')\n    assert True\n"), "test-only"
    )
    assert not p.queue and not p.can_submit


class TargetSandbox(PublicSandbox):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        (root / "core.py").write_text(BASE["core.py"])

    def execute(self, handle: str, command: str, timeout: float) -> Observation:
        if command in {"add-target", "bad-source", "good-source"}:
            self.commands.append(command)
            if command == "add-target":
                (self.root / "tests/test_target.py").write_text(TARGET)
            else:
                (self.root / "core.py").write_text(BAD if command == "bad-source" else GOOD)
            return Observation("edited", 0)
        if "CTXROUTE_PUBLIC_SELECTION" in command:
            current = {
                str(p.relative_to(self.root)): p.read_text() for p in self.root.rglob("*.py")
            }
            changed = [p for p, s in current.items() if BASE.get(p) != s]
            return Observation(
                json.dumps(select_tests(BASE, current, changed, target_contract=True)), 0
            )
        return super().execute(handle, command, timeout)

    def patch(self, handle: str) -> str:
        # The inherited fixture compares against ORIGINAL; this fixture needs its own base.
        import difflib

        return "".join(
            "".join(
                difflib.unified_diff(
                    BASE.get(n, "").splitlines(True),
                    (self.root / n).read_text().splitlines(True)
                    if (self.root / n).exists()
                    else [],
                    fromfile="a/" + n,
                    tofile="b/" + n,
                )
            )
            for n in ["core.py", "tests/test_target.py"]
        )


def test_target_fails_before_fix_wrong_fix_fails_then_repair_and_regressions_pass(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            json.dumps({"tool": "bash", "command": c})
            for c in ["add-target", "bad-source", "good-source"]
        ]
        + ['{"tool":"submit"}']
    )
    writer = RunWriter(tmp_path, "target-cycle", {"is_benchmark": False})
    sandbox = TargetSandbox(tmp_path / "repo")
    result = Engine(config(), model, sandbox, writer).run(
        Task("toy", "toy/repo", "base", "Handle None and retain strings"), 1
    )
    assert result.state.termination == "submitted"
    traces = [json.loads(x) for x in (writer.path / "traces.jsonl").read_text().splitlines()]
    tests = [
        (t["verification_category"], t["exit_code"])
        for t in traces
        if t.get("origin") == "automatic_verification"
    ]
    assert tests == [("target_baseline", 1), ("target", 1), ("target", 0), ("regression", 0)]
    assert len(model.requests) == 4
    assert sandbox.commands.count("bad-source") == sandbox.commands.count("good-source") == 1
    state = json.loads((writer.path / "state.json").read_text())
    assert state["repair_policy"]["target_contract"]["baseline_assertion_proven"]
    assert state["repair_policy"]["validation_ready_to_submit"]


def test_action_history_and_examples_are_standalone_strict_objects(tmp_path: Path) -> None:
    cfg = config()
    context = ContextManager(
        action_prompt(cfg),
        "task",
        cfg.context,
        ArtifactStore(tmp_path, max_bytes=10000, recall_chars=1000),
    )
    context.action_records = True
    context.append(Step(1, "strong", Action("bash", command="echo done"), "done", 0))
    context.append(Step(2, "strong", None, "Action not executed", error="format_error"))
    text = context.messages()[-1]["content"]
    assert "FORMAT CORRECTION FIRST" in text.split("NEXT ACTION")[-1]
    assert '"action":' not in text
    assert '"remember":[' not in action_prompt(cfg)
    assert Action.parse(
        text.split("(history, do not replay)\n")[1].splitlines()[0], recall_enabled=False
    ) == Action("bash", command="echo done")


def test_invalid_trailing_bracket_never_executes_then_valid_action_executes_once(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            '{"tool":"bash","command":"echo once"}]',
            '{"tool":"bash","command":"echo once"}',
            '{"tool":"submit"}',
        ]
    )
    sandbox = FakeSandbox()
    writer = RunWriter(tmp_path, "format-cycle", {"is_benchmark": False})
    result = Engine(config(), model, sandbox, writer).run(
        Task("toy", "toy/repo", "base", "task"), 1
    )
    assert result.state.termination == "incomplete_validation"
    assert sandbox.commands == ["echo once"]
    assert "FORMAT CORRECTION FIRST" in model.requests[1].messages[-1]["content"]
    assert len(model.requests) == 3


def test_strategy_requires_regression_policy() -> None:
    with pytest.raises(ValueError, match="requires"):
        ProgressConfig(target_action_strategy=True)
