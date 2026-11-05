"""Opt-in protocol recovery never invents commands or promotes unsupported memory."""

import json
from pathlib import Path

import pytest

from ctxroute.config import ArmConfig
from ctxroute.domain import ModelRequest, ModelResponse, Observation, Task
from ctxroute.models.base import ModelFailure
from ctxroute.models.scripted import ScriptedModel
from ctxroute.orchestrator.engine import Engine
from ctxroute.sandbox.fake import FakeSandbox
from ctxroute.tracing.cost import Usage
from ctxroute.tracing.writer import RunWriter

ROOT = Path(__file__).resolve().parents[1]
TASK = Task("synthetic-recovery", "fake/repo", "fixture", "Fix the synthetic issue")


@pytest.mark.parametrize("recovery", [False, True])
def test_bad_optional_memory_cannot_block_valid_edit_when_opted_in(
    tmp_path: Path, recovery: bool
) -> None:
    class EditingSandbox(FakeSandbox):
        def execute(self, handle: str, command: str, timeout: float) -> Observation:
            result = super().execute(handle, command, timeout)
            if command == "edit-fixture":
                self.patch_text = "+synthetic edit\n"
            return result

    model = ScriptedModel(
        [
            '{"tool":"bash","command":"cat fixture"}',
            '{"tool":"bash","command":"edit-fixture",'
            '"remember":[{"step":1,"quote":"invented quote"}]}',
            '{"tool":"submit"}',
        ]
    )
    sandbox = EditingSandbox({"cat fixture": Observation("actual observation", 0)})
    writer = RunWriter(tmp_path, "optional-memory", {"kind": "synthetic_test"})
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"),
        model,
        sandbox,
        writer,
        recover_action_metadata=recovery,
    ).run(TASK, 1)
    assert result.state.termination == "submitted" and sandbox.destroyed
    assert ("edit-fixture" in sandbox.commands) is recovery
    assert bool(result.patch) is recovery
    state = json.loads((writer.path / "state.json").read_text())
    assert "invented quote" not in json.dumps(state["fixed_memory"])
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    if recovery:
        assert traces[1]["memory_validation_error"]
        assert result.state.history[1].action is not None
        assert result.state.history[1].action.remember == ()
    else:
        assert traces[1]["error_type"] == "format_error"


@pytest.mark.parametrize(
    "suffix", ["exact_echo", "changed_echo", "second_action", "brace", "fence"]
)
def test_only_exact_current_prompt_echo_can_be_removed(tmp_path: Path, suffix: str) -> None:
    class Model:
        calls = 0

        def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            if self.calls > 1:
                return ModelResponse('{"tool":"submit"}', "synthetic", Usage(1, 0, 1))
            action = '{"tool":"bash","command":"record-fixture"}'
            marker = "NEXT ACTION (orchestrator guidance, not tool output)\n"
            current = request.messages[-1]["content"].rsplit(marker, 1)[1]
            if suffix == "exact_echo":
                text = action + "\n\n" + marker + current
            elif suffix == "changed_echo":
                changed = json.loads(current)
                changed["guidance"] = "Different guidance"
                text = action + "\n\n" + marker + json.dumps(changed)
            elif suffix == "second_action":
                text = action + '\n{"tool":"bash","command":"unexpected"}'
            elif suffix == "brace":
                text = action + "}"
            else:
                text = "```json\n" + action + "\n```"
            return ModelResponse(text, "synthetic", Usage(1, 0, 1))

    sandbox = FakeSandbox()
    writer = RunWriter(tmp_path, "exact-echo", {"kind": "synthetic_test"})
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"),
        Model(),
        sandbox,
        writer,
        recover_action_metadata=True,
    ).run(TASK, 1)
    assert result.state.termination == "submitted"
    assert sandbox.commands == (["record-fixture"] if suffix == "exact_echo" else [])
    traces = [json.loads(line) for line in (writer.path / "traces.jsonl").read_text().splitlines()]
    if suffix == "exact_echo":
        assert traces[0]["action_recovery"]["exact_prompt_echo_removed"] is True
        assert "NEXT ACTION" in traces[0]["model_response"]
    else:
        assert traces[0]["error_type"] == "format_error"


def test_nontransient_model_failure_is_not_retried_in_recovery_mode(tmp_path: Path) -> None:
    class Rejected:
        calls = 0

        def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            raise ModelFailure("Definitive rejection", retryable=False)

    model = Rejected()
    sandbox = FakeSandbox()
    result = Engine(
        ArmConfig.load(ROOT / "configs/arms/A1.yaml"),
        model,
        sandbox,
        RunWriter(tmp_path, "nontransient", {"kind": "synthetic_test"}),
        retry_transient_only=True,
        model_attempts=2,
    ).run(TASK, 1)
    assert model.calls == 1 and result.state.termination == "model_failure" and sandbox.destroyed
