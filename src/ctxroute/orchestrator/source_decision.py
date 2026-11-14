"""Failure-to-source evidence and a bounded final edit decision, not patch generation."""

import json
import re
from pathlib import PurePosixPath
from typing import Any


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(set(parts[:-1]) & {"test", "tests", "testing", "testutils", "fixtures"}) or (
        PurePosixPath(path).name.startswith("test_")
        or PurePosixPath(path).name.endswith("_test.py")
        or PurePosixPath(path).name == "conftest.py"
    )


def production_patch(patch: str) -> str:
    """Python implementation diff only; test additions never count as source repair."""
    pieces = re.split(
        r"(?m)(?=^diff --git )" if "diff --git " in patch else r"(?m)(?=^--- a/)", patch
    )
    result: list[str] = []
    for piece in pieces:
        match = re.search(r"(?m)^(?:diff --git a/|--- a/)([^\s]+)", piece)
        if match and match[1].endswith(".py") and not is_test_path(match[1]):
            result.append(piece)
    return "".join(result)


class SourceDecision:
    def __init__(self, diagnostic_limit: int) -> None:
        self.limit = diagnostic_limit
        self.diagnostics = 0
        self.first_source_edit_step: int | None = None
        self.source_edits: list[int] = []
        self.failure_repairs: list[int] = []
        self.last_diagnostic: dict[str, Any] = {}
        self.intent: dict[str, Any] = {}
        self.intents: list[dict[str, Any]] = []

    def observe(
        self,
        command: str,
        before: str,
        after: str,
        step: int,
        *,
        model_action: bool,
        had_failure: bool,
        failures: list[dict[str, Any]],
    ) -> None:
        changed = production_patch(before) != production_patch(after)
        if model_action and had_failure:
            notes = dict(
                re.findall(r"(?m)^# (hypothesis|location|failure_step): ([^\n]{1,400})$", command)
            )
            if notes:
                linked = [f for f in failures if str(f["step"]) == notes.get("failure_step")]
                self.intent = {
                    "action_step": step,
                    **notes,
                    "status": "model_hypothesis_not_verified",
                    "failure_link_valid": bool(linked),
                    "failure_evidence": linked,
                    "retest_commands": sorted({f["command"] for f in failures}),
                    "produced_source_change": changed,
                }
                self.intents.append(self.intent)
        if changed:
            if production_patch(after) and self.first_source_edit_step is None:
                self.first_source_edit_step = step
            self.source_edits.append(step)
            if had_failure:
                self.failure_repairs.append(step)
            self.diagnostics = 0
        elif model_action and had_failure:
            # Includes repeated tests and test-only edits: neither buys new diagnostics.
            self.diagnostics += 1
            self.last_diagnostic = {"step": step, "command": command[:1000]}

    def status(self, patch: str, failures: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "first_production_edit_step": self.first_source_edit_step,
            "production_patch_present": bool(production_patch(patch)),
            "production_edit_steps": self.source_edits,
            "failure_driven_source_edit_steps": self.failure_repairs,
            "diagnostics_since_source_change": self.diagnostics,
            "final_decision_due": self.diagnostics == self.limit and bool(failures),
            "last_diagnostic": self.last_diagnostic,
            "model_repair_hypothesis": self.intent or {"status": "not_provided"},
            "failure_steps": [f["step"] for f in failures],
            "retest_commands": sorted({f["command"] for f in failures}),
        }

    def guidance(
        self, patch: str, failures: list[dict[str, Any]], *, detailed_retests: bool = True
    ) -> str:
        brief = self.status(patch, failures)
        brief.pop("model_repair_hypothesis")
        if not detailed_retests:
            brief.pop("retest_commands")
            brief["retest_owner"] = "orchestrator: full target and selected regression checks"
        return (
            "A failing target and a test-only diff do NOT count as an implementation fix. "
            "Use the public failure and inspected source to form one falsifiable cause. "
            "If supported, make a minimal production-source edit now, preserving target tests. "
            "Start the bash command with short comments '# hypothesis: ...', "
            "'# location: path.py::function', '# failure_step: N', then the edit. "
            "These comments record your hypothesis, not proof. No new JSON fields. "
            "If evidence is insufficient, use one focused source read or diagnostic that "
            "answers a specific question; do not list directories, reread the target scaffold "
            "or search the whole repository. Never invent a fix to satisfy a checkpoint. "
            "Edits automatically rerun target tests then selected regressions. "
            "A final decision is allowed after the diagnostic allowance so the newest result "
            "can inform an edit. Another action without a production change then stops as "
            "incomplete; tests, comments in tests and repeated failures do not reset it. "
            "PUBLIC REPAIR EVIDENCE (hypotheses are not established causes): "
            + json.dumps(brief, ensure_ascii=False)
        )
