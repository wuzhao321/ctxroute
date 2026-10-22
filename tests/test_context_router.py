from dataclasses import replace
from pathlib import Path

import pytest

from ctxroute.config import ContextConfig, RouterConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.manager import ContextLimit, ContextManager, Memory
from ctxroute.domain import Action, RunState, Step, Task
from ctxroute.router.rules import RuleRouter, classify


def test_batch_compression_preserves_protected_parts_recent_steps_and_references(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path, max_bytes=100000, recall_chars=4000)
    cfg = ContextConfig(5000, 6, 0.7, 0.9, "summary")
    manager = ContextManager("SYSTEM-KEEP", "TASK-KEEP", cfg, store)
    manager.fixed["confirmed"] = "PINNED-KEEP"
    for n in range(12):
        aid = store.put(f"observation-{n}" + "x" * 2000)
        manager.append(
            Step(n, "strong", Action("bash", command="cat file"), "x" * 2000, 0, artifact_id=aid)
        )
    last_six = manager.recent[-6:]
    calls: list[str] = []

    def compress(text: str) -> Memory:
        calls.append(text)
        return Memory(
            files_read=["file:1-10; confirmed fact"], hypotheses=["Rejected old hypothesis"]
        )

    event = manager.compress(compress)
    assert event.accepted and event.after < event.before
    assert manager.recent == last_six
    assert all(s in manager.serialized() for s in ["SYSTEM-KEEP", "TASK-KEEP", "PINNED-KEEP"])
    manager.compress(compress)
    assert len(calls) == 1
    first_id = next(iter(store.pinned))
    reloaded = ArtifactStore(tmp_path, max_bytes=100000, recall_chars=4000)
    with pytest.raises(ValueError, match="Referenced"):
        reloaded.remove(first_id)
    with pytest.raises(ValueError):
        store.recall("../../.env", 0, 100)
    assert store.recall(first_id, 0, 5) == "obser"


def test_no_loss_when_summary_grows_and_no_repeat_charge(tmp_path: Path) -> None:
    manager = ContextManager(
        "S",
        "T",
        ContextConfig(10000, 1, 0.7, 0.9, "summary"),
        ArtifactStore(tmp_path, max_bytes=10000, recall_chars=100),
    )
    for i in range(3):
        manager.append(Step(i, "strong", Action("bash", command="cat x"), "x" * 1000, 0))
    manager.config = replace(manager.config, budget_tokens=int(manager.tokens() / 0.8))
    before = manager.serialized()
    calls = 0

    def huge(_: str) -> Memory:
        nonlocal calls
        calls += 1
        return Memory(open_questions=["x" * 10000])

    assert not manager.compress(huge).accepted
    assert manager.serialized() == before
    assert not manager.compress(huge).triggered
    assert calls == 1


def test_protected_only_context_stops_instead_of_dropping_system(tmp_path: Path) -> None:
    manager = ContextManager(
        "x" * 1000,
        "task",
        ContextConfig(100, 6, 0.7, 0.9, "summary"),
        ArtifactStore(tmp_path, max_bytes=10000, recall_chars=100),
    )
    with pytest.raises(ContextLimit):
        manager.compress(lambda _: Memory())
    assert manager.system == "x" * 1000


def test_truncation_ablation_never_calls_model(tmp_path: Path) -> None:
    manager = ContextManager(
        "S",
        "T",
        ContextConfig(1000, 1, 0.7, 0.9, "truncate"),
        ArtifactStore(tmp_path, max_bytes=10000, recall_chars=100),
    )
    for i in range(4):
        manager.append(Step(i, "strong", Action("bash", command="cat x"), "x" * 1000, 0))

    def forbidden(_: str) -> Memory:
        raise AssertionError("Truncation must not use the summarizer")

    assert manager.compress(forbidden).accepted


def test_router_has_five_step_lock_and_is_idempotent() -> None:
    state = RunState("r", Task("i", "r", "b", "p"), "A3", 0, step=4)
    state.history = [
        Step(i, "strong", Action("bash", command="cat x"), "ok", 0) for i in range(1, 4)
    ]
    router = RuleRouter(RouterConfig(True, "strong", 3, 5, 10000))
    assert router.decide(state, 100).tier == "cheap"
    state.history.append(Step(4, "cheap", Action("bash", command="sed -i s/a/b/ file"), "ok", 0))
    for i in range(5, 10):
        state.step = i
        decision = router.decide(state, 100)
        assert decision.tier == "strong" and decision.reason == "strong_lock"
        assert router.decide(state, 100) == decision
        state.history.append(Step(i, "strong", Action("bash", command="cat x"), "ok", 0))
    state.step = 10
    assert router.decide(state, 100).tier == "cheap"


@pytest.mark.parametrize(
    "command",
    [
        "cat x > y",
        "find . -delete",
        "find . -exec rm {} ;",
        "rg --pre sh foo",
        "sed -n '1e whoami' f",
        "cat $(cmd)",
    ],
)
def test_routing_never_mistakes_shell_side_effects_for_read(command: str) -> None:
    assert classify(command) == "edit_or_unknown"


@pytest.mark.parametrize(
    "text",
    [
        '{"tool":"recall","id":"a","start":0,"end":2}',
        '{"tool":"submit","command":"rm -rf /"}',
        "[]",
        '{"tool":"bash","command":null}',
    ],
)
def test_strict_action_surface(text: str) -> None:
    with pytest.raises(ValueError):
        Action.parse(text, recall_enabled=False)
