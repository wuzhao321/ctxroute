"""Observable progress only: repeated command/output/patch, never inferred success."""

import hashlib
from decimal import Decimal

from ctxroute.config import ProgressConfig
from ctxroute.domain import Action, Observation


class ProgressTracker:
    def __init__(self, config: ProgressConfig) -> None:
        self.config = config
        self.patch = ""
        self.last_command = ""
        self.last_signature: tuple[str, str, int, str] | None = None
        self.unchanged_repeats = 0
        self.blocked_repeats = 0

    def observe(self, command: str, observation: Observation, patch: str) -> None:
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
