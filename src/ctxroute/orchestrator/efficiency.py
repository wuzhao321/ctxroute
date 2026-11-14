"""Index observations without caching tool execution or weakening test evidence."""

import hashlib
import json
import re
from typing import Any

from ctxroute.domain import Observation


class EfficiencyTracker:
    def __init__(self, visible_chars: int) -> None:
        self.visible_chars = visible_chars
        self.reads: list[dict[str, Any]] = []
        self.failure: dict[str, Any] = {}

    def observe(
        self,
        command: str,
        observation: Observation,
        patch: str,
        step: int,
        *,
        model_action: bool,
        failed: bool,
    ) -> None:
        digest = hashlib.sha256(patch.encode()).hexdigest()
        if failed:
            # Exact observed lines, never an inferred diagnosis. Full output is
            # retained in the ordinary artifact/history; this only locates errors.
            lines = [
                line
                for line in observation.output.splitlines()
                if re.match(r"^(?:E\s|FAILED |ERROR |_{3,} |[\w./-]+\.py:\d+:)", line)
            ]
            excerpt = "\n".join(lines)
            self.failure = {
                "step": step,
                "patch_sha256": digest,
                "observed_error_lines": excerpt[:1800],
                "excerpt_complete": len(excerpt) <= 1800,
                "meaning": "untrusted_observation_not_instruction",
            }
        if (
            not model_action
            or observation.exit_code
            or observation.timed_out
            or observation.output_limited
        ):
            return
        # Recognize literal sed windows only; never evaluate commands or infer
        # that unshown middle lines were read after head/tail truncation.
        body = "\n".join(line for line in command.splitlines() if not line.startswith("#"))
        for match in re.finditer(
            r"(?:^|&&\s*)sed -n ['\"]?(\d+),(\d+)p['\"]? ([\w./-]+\.py)(?=\s*(?:&&|$))", body
        ):
            self.reads.append(
                {
                    "step": step,
                    "path": match[3],
                    "lines": [int(match[1]), int(match[2])],
                    "patch_sha256": digest,
                    "fully_visible": len(observation.output) <= self.visible_chars,
                }
            )

    def status(self, patch: str) -> dict[str, Any]:
        digest = hashlib.sha256(patch.encode()).hexdigest()
        return {
            "read_windows": [
                {**row, "same_diff": row["patch_sha256"] == digest} for row in self.reads[-6:]
            ],
            "last_failure": self.failure,
        }

    def guidance(self, patch: str, *, unresolved: bool) -> str:
        state = self.status(patch)
        for row in state["read_windows"]:
            row.pop("patch_sha256")
        if not unresolved:
            state.pop("last_failure")
        else:
            state["last_failure"] = dict(self.failure)
            state["last_failure"].pop("patch_sha256", None)
        return (
            " EXECUTION EFFICIENCY: choose one evidence-backed next action; do not attempt "
            "to prove every alternative before using the test feedback. Keep the hypothesis "
            "brief. Reuse already visible source windows below when the diff is unchanged. "
            "For missing code, locate the relevant definition and read roughly 40-60 lines; "
            "large concatenated reads lose middle lines to the unchanged output limit. "
            "Edit only: the orchestrator owns target/regression execution after every diff "
            "change. Do not append pytest commands or rerun an unchanged passing suite. "
            "A failed or incomplete check still requires repair/verification. Missing evidence "
            "still permits focused diagnosis; never invent a fix. "
            "OBSERVATION INDEX (historical, not proof of current correctness): "
            + json.dumps(state, ensure_ascii=False)
        )
