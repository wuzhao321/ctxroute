"""Observable progress only: repeated command/output/patch, never inferred success."""

import hashlib
import json
import re
import shlex
from decimal import Decimal

from ctxroute.config import ProgressConfig
from ctxroute.domain import Action, Observation
from ctxroute.orchestrator.verification import CheckEvidence, observe_check
from ctxroute.router.rules import classify


class ProgressTracker:
    def __init__(self, config: ProgressConfig) -> None:
        self.config = config
        self.patch = ""
        self.last_command = ""
        self.last_command_step = 0
        self.last_signature: tuple[str, str, int, str] | None = None
        self.unchanged_repeats = 0
        self.blocked_repeats = 0
        self.executed_commands = 0
        self.verification = "not_run"
        self.verified_patch = ""
        self.test_command = ""
        self.test_step = 0
        self.checks: list[CheckEvidence] = []
        self.last_check: CheckEvidence | None = None
        self.reported_check_exit: int | None = None
        self.reported_check_step: int | None = None
        self.reported_check_changed_patch = False
        self.first_edit_step: int | None = None
        self.source_read_step: int | None = None
        self.source_file = ""
        self.diagnostic_step: int | None = None
        self.edit_checkpoint_step: int | None = None
        self.no_edit_commands = 0

    def observe(
        self, command: str, observation: Observation, patch: str, *, step: int | None = None
    ) -> None:
        self.executed_commands += 1
        number = self.executed_commands if step is None else step
        changed = patch != self.patch
        if changed and patch.strip() and self.first_edit_step is None:
            self.first_edit_step = number
        self._observe_edit_evidence(command, observation, number)
        if self.config.first_edit_checkpoint and self.first_edit_step is None and not patch.strip():
            if self.edit_checkpoint_step is not None:
                self.no_edit_commands += 1
            elif (
                self.executed_commands >= self.config.edit_prompt_after
                and self.source_read_step is not None
                and self.diagnostic_step is not None
            ):
                self.edit_checkpoint_step = number
        if changed:
            self.verification, self.verified_patch = "not_run", ""
            self.test_command, self.test_step = "", 0
            self.reported_check_exit, self.reported_check_step = None, None
            self.reported_check_changed_patch = False
            self.checks.clear()
        # A test that edits the diff does not verify that resulting diff. A
        # recognized test command's exit code is evidence, not official grading.
        self.last_check = observe_check(command, observation, self.patch, patch, number)
        if self.last_check is not None:
            if self.last_check.outcome == "passed" or self.last_check.outcome == "failed":
                self.diagnostic_step = number
            if patch.strip() and not changed:
                check = self.last_check
                self.checks = [
                    old
                    for old in self.checks
                    if not (
                        check.covers(old)
                        and (
                            check.outcome == "passed"
                            or (
                                old.scope == check.scope
                                and (old.outcome != "failed" or check.outcome == "failed")
                            )
                        )
                    )
                ]
                self.checks.append(check)
                failure = next((c for c in reversed(self.checks) if c.outcome == "failed"), None)
                unexecuted = next(
                    (c for c in reversed(self.checks) if c.outcome == "not_run"), None
                )
                effective = failure or unexecuted or check
                self.verification = effective.outcome
                self.verified_patch = (
                    effective.patch_sha256_after if self.verification != "not_run" else ""
                )
                self.test_command, self.test_step = effective.command, effective.step
        child_exit = self.reported_child_exit(command, observation)
        if patch.strip() and child_exit is not None:
            self.reported_check_exit = child_exit or None
            self.reported_check_step = number if child_exit else None
            self.reported_check_changed_patch = changed
            if child_exit:
                # Output is a reported failure signal, not proof about check/edit ordering.
                # In particular, a successful wrapper must not promote it to test success.
                self.verification, self.verified_patch = "not_run", ""
                self.test_command, self.test_step = "", 0
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
        self.last_command_step = number
        self.blocked_repeats = 0

    @staticmethod
    def reported_child_exit(command: str, observation: Observation) -> int | None:
        """Recognize the observed Python wrapper's terminal return-code report only.

        This is untrusted output evidence for guidance, not an execution/security
        classifier or a general parser for shell programs and test frameworks.
        """
        if observation.timed_out or observation.output_limited or ".returncode" not in command:
            return None
        try:
            first = shlex.split(command.splitlines()[0])
        except (ValueError, IndexError):
            return None
        if not first or first[0] not in {"python", "python3"}:
            return None
        match = re.search(r"(?:^|\n)exit\s+(-?\d{1,10})\s*\Z", observation.output)
        return int(match[1]) if match else None

    def _observe_edit_evidence(self, command: str, observation: Observation, step: int) -> None:
        """Record observable candidate evidence, never infer a bug location or diagnosis.

        Only simple Python-source reads and completed test/Python commands are
        recognized. Compound shell programs remain outside this heuristic.
        This does not classify execution safety or alter the command.
        """
        if observation.timed_out or observation.output_limited or not observation.output.strip():
            return
        category = classify(command)
        if category == "read" and observation.exit_code == 0:
            # classify() has already checked that any && prefix is a literal cd.
            read_command = command.split("&&", 1)[-1]
            words = shlex.split(read_command)
            if words[0] in {"cat", "head", "tail", "sed"} and words[-1].endswith(".py"):
                self.source_read_step, self.source_file = step, words[-1]
        if category == "test" and observation.exit_code in {0, 1}:
            self.diagnostic_step = step
        elif observation.exit_code == 0:
            # The first line also covers python - <<'PY' without parsing its body.
            try:
                words = shlex.split(command.splitlines()[0])
            except (ValueError, IndexError):
                return
            if len(words) >= 2 and words[0] in {"python", "python3"}:
                if words[1] in {"-", "-c", "-m"} or words[1].endswith(".py"):
                    self.diagnostic_step = step

    @property
    def edit_checkpoint_exhausted(self) -> bool:
        return (
            self.config.first_edit_checkpoint
            and self.edit_checkpoint_step is not None
            and self.first_edit_step is None
            and self.no_edit_commands >= self.config.max_no_edit_commands
        )

    def edit_metrics(self) -> dict[str, str | int | bool | None]:
        """Trace-only for the default policy, so instrumentation does not alter its prompt."""
        return {
            "first_edit_step": self.first_edit_step,
            "source_read_step": self.source_read_step,
            "candidate_source_file": self.source_file or None,
            "diagnostic_step": self.diagnostic_step,
            "checkpoint_enabled": self.config.first_edit_checkpoint,
            "checkpoint_step": self.edit_checkpoint_step,
            "no_edit_commands_after_checkpoint": self.no_edit_commands,
            "checkpoint_exhausted": self.edit_checkpoint_exhausted,
        }

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

    def repeat_feedback(self) -> str:
        """Describe observed repetition, without changing detection or counting an edit."""
        if not self.blocked_repeats and self.unchanged_repeats < self.config.repeat_warn_after:
            return ""
        exit_code = self.last_signature[2] if self.last_signature is not None else "unknown"
        feedback = (
            f"The last command has the same output, exit code and diff across "
            f"{self.unchanged_repeats} completed executions; it last executed at step "
            f"{self.last_command_step} with exit code {exit_code}. "
        )
        if self.blocked_repeats:
            remaining = max(0, self.config.max_blocked_repeats - self.blocked_repeats)
            feedback += "The rejected proposal was NOT executed and produced no new result. "
            feedback += (
                f"{remaining} more consecutive blocked repeat(s) will stop this run. "
                if remaining
                else "The consecutive blocked-repeat limit has been reached. "
            )
        elif self.unchanged_repeats >= self.config.repeat_block_after:
            feedback += "Repeating this command now will be rejected. "
        feedback += "Use the already recorded result; do not rerun the unchanged command. "
        if not self.patch.strip():
            feedback += (
                "Choose a new discriminating check or an evidence-supported implementation edit. "
                "Changing command spelling or rewriting identical text is not progress. "
            )
        else:
            feedback += (
                "Do not change the diff just to reset repetition. "
                "Follow the current diff's verification and submission requirements. "
            )
        return feedback

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
        elif self.edit_checkpoint_step is not None and self.first_edit_step is None:
            priority = "make_first_edit"
            allowance = max(0, self.config.max_no_edit_commands - self.no_edit_commands)
            next_action = (
                f"First-edit checkpoint: a successful source read at step {self.source_read_step} "
                f"named {self.source_file!r}; a diagnostic command completed at step "
                f"{self.diagnostic_step}. These observations do not establish the cause. "
                "Use the evidence to make a minimal justified implementation edit now. "
                "If a specific uncertainty prevents an edit, resolve it with one focused check. "
                f"Only {allowance} further completed bash commands may return an empty diff "
                "before this run stops. A changed read range, another reproduction, or an "
                "exit code 0 without a repository diff is not an edit. "
                "Do not make irrelevant changes merely to satisfy this checkpoint. "
                "Keep the existing JSON action format."
            )
        elif (
            self.config.first_edit_checkpoint
            and self.first_edit_step is None
            and self.executed_commands >= self.config.edit_prompt_after
        ):
            if self.source_read_step is None:
                priority = "locate_source"
                next_action = (
                    "No simple Python-source read has been recorded. Locate the relevant "
                    "implementation and read one narrow source range before choosing an edit. "
                    "A file listing or broad search alone is not a source inspection."
                )
            else:
                priority = "diagnose_before_edit"
                next_action = (
                    f"Source candidate {self.source_file!r} was read at step "
                    f"{self.source_read_step}, but no completed diagnostic is recorded. "
                    "Run one focused reproduction or repository test to compare the expected "
                    "and observed behavior. Use that result to choose a minimal edit; "
                    "do not keep expanding source reads without a specific question."
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
        if self.config.post_edit_checkpoint:
            if self.patch.strip():
                priority, next_action = self._post_edit_guidance()
            elif priority in {"make_first_edit", "edit_needed"}:
                next_action += (
                    " Before changing a shared function, identify the existing input behavior "
                    "its callers must retain and the relevant public tests. Preserve that behavior "
                    "while fixing the reported case; a broader rewrite needs specific evidence."
                )
        if self.patch.strip() and self.reported_check_exit is not None:
            priority = "repair_patch"
            next_action = (
                f"Step {self.reported_check_step} reported child exit {self.reported_check_exit}. "
                "Inspect that failed reproduction before deciding the patch is ready. "
                "The wrapper's exit code is not the child result. Use a focused check that "
                "propagates failure, then repair the evidenced defect and retest; "
                "do not repeat successful source reads without a specific question."
            )
            if self.reported_check_changed_patch:
                next_action += (
                    " That command also changed the diff; the signal alone does not establish "
                    "whether the check ran before or after the edit. Confirm the current diff."
                )
        unexecuted = next((c for c in reversed(self.checks) if c.outcome == "not_run"), None)
        if unexecuted is not None:
            priority, next_action = unexecuted.unexecuted_guidance()
        repeat_feedback = self.repeat_feedback()
        if repeat_feedback:
            next_action = repeat_feedback + next_action
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
        status: dict[str, str | int | float | bool] = {
            "step": step,
            "next_priority": priority,
            "next_action": next_action,
            "executed_commands": self.executed_commands,
            "local_test_status": self.verification,
            "local_test_patch_sha256": self.verified_patch,
            "local_test_evidence": json.dumps([c.record() for c in self.checks]),
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
        if self.config.post_edit_checkpoint:
            status.update(
                local_test_command=self.test_command,
                local_test_step=self.test_step,
                test_coverage="not_established_by_exit_code",
            )
        if self.reported_check_exit is not None:
            status.update(
                reported_child_exit_code=self.reported_check_exit,
                reported_check_step=self.reported_check_step or 0,
                reported_check_changed_patch=self.reported_check_changed_patch,
            )
        return status

    def _post_edit_guidance(self) -> tuple[str, str]:
        """Guide scope and submission decisions without claiming test coverage."""
        if self.verification == "not_run":
            return "verify_regressions", (
                "Review the current diff for changes beyond the reported case. Select the smallest "
                "set of public repository tests covering the changed function and its callers, "
                "including existing input behavior that must be preserved. Run those tests and "
                "the target regression together where possible. A successful reproduction alone "
                "does not check preserved behavior; avoid an unrelated whole-project test run."
            )
        if self.verification == "failed":
            return "repair_patch", (
                "The recorded test command failed on the current diff. Inspect its output: "
                "distinguish assertion failures from collection or environment errors before "
                "editing. Repair only an evidenced defect and rerun the relevant tests. "
                "An environment error is not proof that the patch caused a regression."
            )
        return "submit_decision", (
            "The recorded test command returned exit code 0 for the current diff. Check its "
            "output and scope: did tests actually run, and do they cover both the reported case "
            "and preserved behavior of the changed function and its callers? If a specific "
            "regression check is missing, run only that check. Otherwise, if the diff is relevant "
            'and no known failure remains, submit now with {"tool":"submit"}. Do not repeat '
            "successful checks or broaden exploration without an identified gap. Exit code 0 "
            "does not establish coverage or official correctness."
        )
