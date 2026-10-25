import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from ctxroute.config import ArmConfig
from ctxroute.domain import ModelRequest, ModelResponse, Observation, Task
from ctxroute.evalkit.export import collect_predictions
from ctxroute.models.base import ModelFailure
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.runtime import build_live_engine
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.budget import BudgetStopped, Ledger
from ctxroute.tracing.cost import Usage
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
TASK = Task("synthetic-1", "fake/repo", "fixture", "Fix the synthetic issue")


def writer(path: Path) -> RunWriter:
    return RunWriter(path, "test-run", {"kind": "synthetic_test", "is_benchmark": False})


@pytest.mark.parametrize("arm", ["A1", "A2", "A3", "A4", "A5"])
def test_end_to_end_fake_run_and_prediction(arm: str, tmp_path: Path) -> None:
    model = ScriptedModel(['{"tool":"bash","command":"cat x"}', '{"tool":"submit"}'])
    sandbox = FakeSandbox(patch="synthetic patch")
    output = writer(tmp_path)
    result = Engine(ArmConfig.load(ROOT / f"configs/arms/{arm}.yaml"), model, sandbox, output).run(
        TASK, 1
    )
    assert result.state.termination == "submitted" and result.patch == "synthetic patch"
    assert all(r.response_format == "json_object" for r in model.requests)
    assert sandbox.destroyed and not sandbox.active
    assert len((output.path / "traces.jsonl").read_text().splitlines()) == 2
    prediction = json.loads((output.path / "predictions.jsonl").read_text())
    assert (
        prediction["instance_id"] == TASK.instance_id and prediction["model_patch"] == result.patch
    )
    with pytest.raises(ValueError, match="Synthetic"):
        collect_predictions([output.path], tmp_path / "submission.jsonl")


def test_compression_calls_cheap_layer_with_same_task_budget(tmp_path: Path) -> None:
    cfg = ArmConfig.load(ROOT / "configs/arms/A4.yaml")
    cfg = replace(
        cfg,
        context=replace(cfg.context, budget_tokens=4000),
        limits=replace(cfg.limits, output_head_chars=1000, output_tail_chars=1000),
    )
    # Distinct reads exercise compression without deliberately triggering the loop guard.
    commands = [f"cat file{i}" for i in range(15)]
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": c}) for c in commands] + ['{"tool":"submit"}']
    )
    sandbox = FakeSandbox({c: Observation("x" * 1500, 0) for c in commands})
    result = Engine(cfg, model, sandbox, writer(tmp_path)).run(TASK, 1)
    assert result.state.termination == "submitted"
    summaries = [r for r in model.requests if r.purpose == "compression"]
    assert summaries and all(r.tier == "cheap" and r.scope_limit == D("0.20") for r in summaries)
    assert all(r.response_format == "json_object" for r in summaries)
    assert any(r.tier == "cheap" for r in model.requests if r.purpose == "action")


def test_format_failure_cleanup_and_corrective_feedback(tmp_path: Path) -> None:
    model = ScriptedModel(["bad", "still bad"])
    sandbox = FakeSandbox()
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"), model, sandbox, writer(tmp_path)
    ).run(TASK, 1)
    assert result.state.termination == "format_failure" and sandbox.destroyed
    assert "Invalid action" in model.requests[1].messages[1]["content"]
    assert "Return exactly one JSON action object" in model.requests[1].messages[1]["content"]
    recent = model.requests[1].messages[1]["content"].split("P3 RECENT STEPS\n", 1)[1]
    assert '{"tool":"bash","command":"..."}' in json.loads(recent)["observation"]


def test_invalid_action_never_executes_before_one_correction(tmp_path: Path) -> None:
    # Valid JSON can still violate the action schema; JSON mode is not schema mode.
    # Extra braces and XML remain invalid, never silently repaired.
    from ctxroute.domain import Action

    for raw in ('{"tool":"submit"}}', '<action>{"tool":"submit"}</action>'):
        with pytest.raises(ValueError):
            Action.parse(raw, recall_enabled=False)
    model = ScriptedModel(
        ['{"tool":"bash","command":"touch unexpected","extra":true}', '{"tool":"submit"}']
    )
    sandbox = FakeSandbox()
    output = writer(tmp_path)
    result = Engine(ArmConfig.load(ROOT / "configs/arms/A1.yaml"), model, sandbox, output).run(
        TASK, 1
    )
    assert result.state.termination == "submitted" and len(model.requests) == 2
    assert result.state.history[0].action is None
    assert sandbox.commands == []
    traces = [json.loads(line) for line in (output.path / "traces.jsonl").read_text().splitlines()]
    assert [t["tool"] for t in traces] == [None, "submit"]
    assert traces[0]["validation_error"] == "Invalid action schema or unavailable tool"


def test_two_model_failures_stop_and_cleanup(tmp_path: Path) -> None:
    class FailedModel:
        calls = 0

        def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            raise ModelFailure("simulated failure")

    model = FailedModel()
    sandbox = FakeSandbox()
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"),
        model,
        sandbox,
        writer(tmp_path),
        sleep=lambda _: None,
    ).run(TASK, 1)
    assert result.state.termination == "model_failure" and model.calls == 2 and sandbox.destroyed


def test_timeout_stops_and_cleans_owned_sandbox(tmp_path: Path) -> None:
    sandbox = FakeSandbox({"cat x": Observation("timeout", 124, timed_out=True)})
    model = ScriptedModel(['{"tool":"bash","command":"cat x"}'])
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"), model, sandbox, writer(tmp_path)
    ).run(TASK, 1)
    assert result.state.termination == "command_timeout" and sandbox.destroyed


def test_task_budget_includes_compression_and_unsettled_usage(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "budget.sqlite", hard_limit=D("10"), pause_at=D("8"), enabled=True)
    ledger.reserve("action", D("0.12"), "model_api", scope="task", scope_limit=D("0.20"))
    ledger.settle("action", None)
    with pytest.raises(BudgetStopped, match="Task budget"):
        ledger.reserve("compression", D("0.09"), "model_api", scope="task", scope_limit=D("0.20"))
    with pytest.raises(ValueError, match="cannot change"):
        ledger.reserve("retry", D("0.09"), "model_api", scope="task", scope_limit=D("0.50"))


def test_offline_mode_rejects_live_factory_without_reading_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "runtime.json").write_text('{"execution_mode":"offline_only"}')
    (configs / "budget.json").write_text('{"paid_execution_enabled":true}')

    def forbidden_key_read(root: Path) -> str:
        raise AssertionError("Offline mode must reject before reading credentials")

    monkeypatch.setattr("ctxroute.runtime.load_key", forbidden_key_read)
    with pytest.raises(RuntimeError, match="code-only"):
        build_live_engine(tmp_path, "A1", TASK, FakeSandbox())


def test_recorded_over_budget_response_terminates(tmp_path: Path) -> None:
    class Expensive:
        def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                '{"tool":"submit"}', "synthetic", Usage(1, 0, 1), cost_upper=D("0.21")
            )

    sandbox = FakeSandbox()
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"), Expensive(), sandbox, writer(tmp_path)
    ).run(TASK, 1)
    assert result.state.termination == "budget_limit" and sandbox.destroyed


def test_unchanged_loop_blocks_execution_but_accounts_for_model_calls(tmp_path: Path) -> None:
    class CostedFixture(ScriptedModel):
        def complete(self, request: ModelRequest) -> ModelResponse:
            return replace(super().complete(request), cost_upper=D("0.001"))

    model = CostedFixture(['{"tool":"bash","command":"python -c \'print(1)\'"}'] * 5)
    sandbox = FakeSandbox()
    output = writer(tmp_path)
    result = Engine(ArmConfig.load(ROOT / "configs/arms/A1.yaml"), model, sandbox, output).run(
        TASK, 1
    )
    assert result.state.termination == "stagnation" and sandbox.destroyed
    assert len(sandbox.commands) == 3 and len(model.requests) == 5
    assert result.state.accounted_cost == D("0.005")
    assert "Repeating the last command" in model.requests[2].messages[1]["content"]
    traces = [json.loads(s) for s in (output.path / "traces.jsonl").read_text().splitlines()]
    assert [t["executed"] for t in traces] == [True, True, True, False, False]
    assert traces[-1]["progress"]["actions_left_including_current"] == 46
    assert traces[-1]["progress"]["patch_present"] is False
    assert traces[-1]["error_type"] == "repeated_action_blocked"


def test_loop_feedback_allows_recovery_and_changed_patch_resets_streak(tmp_path: Path) -> None:
    class EditingFixture(FakeSandbox):
        def execute(self, handle: str, command: str, timeout: float) -> Observation:
            result = super().execute(handle, command, timeout)
            if command == "edit-fixture":
                self.patch_text += "+changed line\n"
            return result

    cfg = ArmConfig.load(ROOT / "configs/arms/A4.yaml")
    # Exercise the final-action reminder without running 50 fixture requests.
    cfg = replace(cfg, progress=replace(cfg.progress, final_step_warning=50))
    actions = ["cat x"] * 4 + ["edit-fixture"] * 5
    model = ScriptedModel(
        [json.dumps({"tool": "bash", "command": c}) for c in actions] + ['{"tool":"submit"}']
    )
    sandbox = EditingFixture()
    output = writer(tmp_path)
    result = Engine(cfg, model, sandbox, output).run(TASK, 1)
    assert result.state.termination == "submitted" and sandbox.destroyed
    assert len(sandbox.commands) == 8 and result.patch.count("+changed line") == 5
    assert "Command not executed" in model.requests[4].messages[1]["content"]
    assert "Few actions remain" in model.requests[-1].messages[1]["content"]
    traces = [json.loads(s) for s in (output.path / "traces.jsonl").read_text().splitlines()]
    assert traces[-1]["progress"]["patch_present"] is True
    assert traces[-1]["progress"]["patch_added_lines"] == 5
    assert model.requests[3].tier == "cheap" and model.requests[4].tier == "strong"
