"""Observable progress only: repeated command/output/patch, never inferred success."""

import hashlib
from decimal import Decimal

from ctxroute.config import ProgressConfig
from ctxroute.domain import Action, Observation
from ctxroute.router.rules import classify


class ProgressTracker:
    def __init__(self, config: ProgressConfig) -> None:
        self.config = config
        self.patch = ""
        self.last_command = ""
        self.last_signature: tuple[str, str, int, str] | None = None
        self.unchanged_repeats = 0
        self.blocked_repeats = 0
        self.executed_commands = 0
        self.verification = "not_run"
        self.verified_patch = ""

    def observe(self, command: str, observation: Observation, patch: str) -> None:
        self.executed_commands += 1
        changed = patch != self.patch
        if changed:
            self.verification, self.verified_patch = "not_run", ""
        # A test that edits the diff does not verify that resulting diff. A
        # recognized test command's exit code is evidence, not official grading.
        if patch.strip() and not changed and classify(command) == "test":
            self.verification = "passed" if observation.exit_code == 0 else "failed"
            self.verified_patch = hashlib.sha256(patch.encode()).hexdigest()
        signature = (
            command,
            hashlib.sha256(observation.output.encode()).hexdigest(),
            observation.exit_code,
            hashlib.sha256(patch.encode()).hexdigest(),
        )
        self.unchanged_repeats = (
            self.unchanged_repeats + 1 if signature == self.last_signature else 1
        )
        self.last_signature, self.last_command, self.patch = signature, command, patch
        self.blocked_repeats = 0

    def blocks(self, action: Action) -> bool:
        if (
            action.kind == "bash"
            and action.command == self.last_command
            and self.unchanged_repeats >= self.config.repeat_block_after
        ):
            self.blocked_repeats += 1
            return True
        self.blocked_repeats = 0
        return False

    def status(
        self, *, step: int, max_steps: int, seconds_left: float, cost_left: Decimal
    ) -> dict[str, str | int | float | bool]:
        remaining = max_steps - step + 1
        if self.patch.strip():
            priority = "verify_patch"
            next_action = "Run a focused repository test covering the changed behavior."
            if self.verification == "passed":
                priority = "review_and_submit"
                next_action = (
                    "A recognized test command returned exit code 0 for the current diff. "
                    "Review the diff for relevance and submit if ready; "
                    "official grading is pending."
                )
            elif self.verification == "failed":
                priority = "repair_patch"
                next_action = (
                    "Inspect the failed test output and revise the patch before retesting."
                )
        elif (
            self.executed_commands >= self.config.edit_prompt_after
            or self.unchanged_repeats >= self.config.repeat_warn_after
        ):
            priority = "edit_needed"
            next_action = (
                "No repository diff exists. Use the inspected source to make a minimal justified "
                "implementation edit. If the cause is still unclear, inspect a different relevant "
                "source section to answer one specific question, then edit. "
                "Repeating the reproduction without a changed hypothesis is not an edit."
            )
        else:
            priority = "investigate"
            next_action = (
                "Locate the relevant implementation and compare expected with observed behavior."
            )
        advice = "Investigate, edit the repository, run a focused check, then submit the patch."
        if self.unchanged_repeats >= self.config.repeat_warn_after:
            advice += (
                " Repeating the last command has returned identical output and patch. "
                "Use a different diagnostic or make an informed edit."
            )
        if remaining <= self.config.final_step_warning:
            advice += (
                " Few actions remain: prioritize a concrete edit and focused verification. "
                'Submit with {"tool":"submit"} once ready; submission does not verify correctness.'
            )
        return {
            "step": step,
            "next_priority": priority,
            "next_action": next_action,
            "executed_commands": self.executed_commands,
            "local_test_status": self.verification,
            "local_test_patch_sha256": self.verified_patch,
            "actions_left_including_current": remaining,
            "seconds_left": round(max(0.0, seconds_left), 1),
            "local_cost_allowance_left_usd": str(max(Decimal(0), cost_left)),
            "budget_note": "Conservative local allowance; ledger also guards pending charges.",
            "patch_present": bool(self.patch.strip()),
            "patch_added_lines": sum(
                line.startswith("+") and not line.startswith("+++")
                for line in self.patch.splitlines()
            ),
            "patch_removed_lines": sum(
                line.startswith("-") and not line.startswith("---")
                for line in self.patch.splitlines()
            ),
            "same_command_output_patch_streak": self.unchanged_repeats,
            "consecutive_blocked_repeats": self.blocked_repeats,
            "advice": advice,
        }
