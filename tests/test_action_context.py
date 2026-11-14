"""Model-visible actions round-trip through the same strict execution protocol."""

import json
from dataclasses import asdict

import pytest

from ctxroute.domain import Action, EvidenceNote, Step


@pytest.mark.parametrize(
    "action",
    [
        Action("bash", command="python -c 'print(\"观察\")'"),
        Action("bash", command="pytest tests", remember=(EvidenceNote(1, "observed output"),)),
        Action("recall", artifact_id="fixture", start=10, end=30),
        Action("submit"),
    ],
)
def test_context_uses_valid_protocol_without_changing_audit_records(action: Action) -> None:
    step = Step(2, "strong", action, "tool observation", 0)
    payload = json.loads(step.text())["action"]
    assert Action.parse(json.dumps(payload), recall_enabled=True) == action
    assert "kind" not in payload and "artifact_id" not in payload
    if action.kind != "recall":
        assert "start" not in payload and "end" not in payload
    # Internal state snapshots keep their existing format for audit/readers.
    assert asdict(step)["action"]["kind"] == action.kind
    assert asdict(step)["action"]["start"] == action.start


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"tool":"bash","command":"echo check"}]', "Unexpected trailing content"),
        ('{"tool":"bash","command":"echo check","start":0}', "Unsupported fields for bash: start"),
        (
            '{"tool":"submit"}\n{"tool":"bash","command":"echo check"}',
            "Unexpected trailing content",
        ),
    ],
)
def test_observed_format_failures_stay_rejected_with_specific_feedback(
    raw: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Action.parse(raw, recall_enabled=False)
