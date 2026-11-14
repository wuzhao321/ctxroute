"""Optional regression execution and honest completion decisions.

Existing Checkpoint behavior is untouched when this policy is disabled. Every
automatic test uses a normal bounded bash step and a single pytest invocation.
"""

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from ctxroute.config import ProgressConfig
from ctxroute.domain import Observation
from ctxroute.orchestrator.efficiency import EfficiencyTracker
from ctxroute.orchestrator.progress import ProgressTracker
from ctxroute.orchestrator.public_behavior import STAGED_PATH, TARGET_PATH, contract_sha
from ctxroute.orchestrator.repair_evidence import RepairEvidence
from ctxroute.orchestrator.source_decision import SourceDecision, production_patch
from ctxroute.orchestrator.target_contract import TargetContract
from ctxroute.orchestrator.verification import CheckEvidence, observe_check


@dataclass(frozen=True)
class VerificationJob:
    category: str
    command: str
    selectors: tuple[str, ...]


def selector_command(base: str, config: ProgressConfig) -> str:
    script = Path(__file__).with_name("public_tests.py").read_text()
    return (
        "python - "
        + shlex.join(
            [base, str(config.regression_caller_depth), str(config.regression_max_files)]
            + (["1"] if config.target_action_strategy else [])
        )
        + " <<'CTXROUTE_PUBLIC_SELECTION'\n"
        + script
        + "\nCTXROUTE_PUBLIC_SELECTION"
    )


class RepairPolicy:
    def __init__(self, config: ProgressConfig, *, visible_output_chars: int = 4000) -> None:
        self.config = config
        self.patch = ""
        self.plan: dict[str, Any] = {}
        self.queue: list[VerificationJob] = []
        self.results: list[dict[str, Any]] = []
        self.unresolved: dict[str, CheckEvidence] = {}
        self.failure_output = ""
        self.diagnostics = 0
        self.last_failure_step: int | None = None
        self.failure_repairs = 0
        self.selection_error = ""
        self.pre_edit_checks: list[dict[str, Any]] = []
        self.source = (
            SourceDecision(config.repair_diagnostic_limit) if config.source_edit_strategy else None
        )
        self.efficiency = (
            EfficiencyTracker(visible_output_chars) if config.efficiency_strategy else None
        )
        self.evidence = (
            RepairEvidence(visible_output_chars, public_scaffold=config.delivery_behavior_strategy)
            if config.evidence_action_strategy
            else None
        )
        self.target_contract = (
            TargetContract(allow_controls=config.delivery_behavior_strategy)
            if config.target_action_strategy
            else None
        )

    def _test_command(self, selectors: tuple[str, ...] | list[str]) -> str:
        options = (
            ["-vv", "--showlocals", "--tb=short"]
            if self.efficiency or self.evidence
            else ["-q", "--tb=short"]
        )
        return shlex.join(["python", "-m", "pytest", *options, *selectors])

    def install_plan(self, raw: str, patch: str) -> None:
        raw_value: Any = json.loads(raw)
        if not isinstance(raw_value, dict):
            raise ValueError("Invalid public selector result")
        value = cast(dict[str, Any], raw_value)
        if not isinstance(value.get("selection_complete"), bool):
            raise ValueError("Invalid public selector result")
        self.plan = value
        self.patch = patch
        self.queue.clear()
        self.results.clear()
        self.selection_error = str(value.get("selection_issue", ""))
        if self.config.delivery_behavior_strategy:
            info = value.get("target_contract", {})
            if info.get("file_sha256") != {TARGET_PATH: contract_sha()}:
                info["test_only_valid"] = False
                value["selection_complete"] = False
                value["selection_issue"] = "Use the unchanged public external-behavior scaffold"
        if self.target_contract is not None:
            self.target_contract.select(value)
        categories = ("target", "regression") if self.target_contract else ("regression", "target")
        for category in categories:
            raw_selectors = value.get(category)
            if not isinstance(raw_selectors, list):
                raise ValueError("Invalid public test selectors")
            items = cast(list[Any], raw_selectors)
            if any(
                not isinstance(s, str)
                or re.fullmatch(r"tests/[a-zA-Z0-9_./-]+\.py(?:::[a-zA-Z0-9_]+)*", s) is None
                or ".." in s.split("/")
                for s in items
            ):
                raise ValueError("Unsafe public test selector")
            selectors = cast(list[str], items)
            baseline = (
                self.target_contract is not None
                and self.target_contract.baseline_candidate
                and not self.target_contract.red_proven
            )
            if baseline and category == "target":
                for selector in selectors:
                    self.queue.append(
                        VerificationJob(
                            "target_baseline",
                            self._test_command([selector]),
                            (selector,),
                        )
                    )
            elif value["selection_complete"] and selectors and not baseline:
                # Separate files keep each command's runtime and error output
                # bounded, while pytest reports every selected case in that file.
                for file in sorted({s.split("::")[0] for s in selectors}):
                    chosen = tuple(s for s in selectors if s.split("::")[0] == file)
                    command = self._test_command(chosen)
                    self.queue.append(VerificationJob(category, command, chosen))

    def observe(
        self,
        command: str,
        observation: Observation,
        before: str,
        after: str,
        step: int,
        *,
        job: VerificationJob | None = None,
    ) -> None:
        previous_failures = [c.record() for c in self.unresolved.values()]
        changed = before != after
        if self.target_contract and job and job.category == "target_baseline":
            self.target_contract.observe(job.selectors, observation, before, after, step)
        if changed:
            if self.unresolved:
                self.failure_repairs += 1
            self.diagnostics = 0
        check = observe_check(command, observation, before, after, step)
        child_exit = ProgressTracker.reported_child_exit(command, observation)
        if check is None and child_exit:
            check = CheckEvidence(
                command,
                hashlib.sha256(command.encode()).hexdigest(),
                (),
                "diagnostic",
                (),
                "failed",
                observation.exit_code,
                None,
                "reported_child_exit",
                hashlib.sha256(before.encode()).hexdigest(),
                hashlib.sha256(after.encode()).hexdigest(),
                step,
            )
        if not before.strip() and not after.strip():
            if check:
                self.pre_edit_checks.append(check.record())
            return
        if check is not None and job is not None:
            check = replace(check, targets=job.selectors, options=())
        if check is not None:
            if check.outcome == "failed":
                self.unresolved[check.scope] = check
                self.last_failure_step = step
                self.failure_output = observation.output[-5000:]
                self.diagnostics = 0
                # Give the failure to the model immediately. Additional selected
                # tests for this diff stay incomplete until the repair/retest.
                if job is None or job.category != "target_baseline":
                    self.queue.clear()
            elif not changed:
                for scope, old in list(self.unresolved.items()):
                    if check.covers(old):
                        self.unresolved.pop(scope)
        elif self.unresolved and not changed and job is None:
            self.diagnostics += 1
        if job is not None:
            executed = bool(re.search(r"\b[1-9]\d* (?:passed|failed)\b", observation.output))
            passed = bool(
                check
                and check.outcome == "passed"
                and executed
                and not changed
                and not observation.timed_out
                and not observation.output_limited
            )
            self.results.append(
                {
                    "category": job.category,
                    "selectors": list(job.selectors),
                    "command": command,
                    "exit_code": observation.exit_code,
                    "tests_executed": executed,
                    "passed": passed,
                    "step": step,
                    "patch_sha256_before": hashlib.sha256(before.encode()).hexdigest(),
                    "patch_sha256_after": hashlib.sha256(after.encode()).hexdigest(),
                }
            )
            if not passed and job.category != "target_baseline":
                self.queue.clear()
        self.patch = after
        if self.evidence:
            self.evidence.observe(
                command,
                observation,
                after,
                step,
                unchanged_model_action=job is None and not changed,
                failed=check is not None and check.outcome == "failed",
            )
        if self.efficiency:
            self.efficiency.observe(
                command,
                observation,
                after,
                step,
                model_action=job is None and not changed,
                failed=check is not None and check.outcome == "failed",
            )
        if self.source:
            self.source.observe(
                command,
                before,
                after,
                step,
                model_action=job is None,
                had_failure=bool(previous_failures),
                failures=previous_failures,
            )

    @property
    def can_submit(self) -> bool:
        if self.source and not production_patch(self.patch):
            return False
        if self.target_contract and not self.target_contract.red_proven:
            return False
        if (
            not self.patch.strip()
            or self.queue
            or self.unresolved
            or not self.plan.get("selection_complete")
        ):
            return False
        expected = {("regression", s) for s in self.plan.get("regression", [])}
        expected.update(("target", s) for s in self.plan.get("target", []))
        covered = {
            (result["category"], selector)
            for result in self.results
            if result["passed"]
            and result["patch_sha256_after"] == hashlib.sha256(self.patch.encode()).hexdigest()
            for selector in result["selectors"]
        }
        # A target regression must be explicit, not inferred from an arbitrary
        # successful reproduction or broad regression suite.
        return bool(self.plan.get("target")) and bool(expected) and expected <= covered

    def status(self, seconds_left: float, actions_left: int) -> dict[str, Any]:
        if self.target_contract and not self.target_contract.red_proven:
            priority = "establish_target_assertion"
            instruction = (
                "Before implementation edits, add a focused test in tests/ from the PUBLIC issue "
                "and reproduction. Give it a docstring stating expected behavior and explicit "
                "assertions. Verify positive functionality, not only absence of errors or empty "
                "output. Use a test-only edit and git add -N for new files. The policy will run "
                "each target separately on unchanged implementation; an assertion must fail. "
                "Collection/import errors or passing baseline tests do not establish the target. "
                "If source was changed prematurely, revert ONLY your implementation edit first. "
                "After a recorded baseline assertion failure, keep that test file unchanged."
            )
            if self.config.delivery_behavior_strategy:
                instruction = (
                    f"Install the public behavior scaffold with cp {STAGED_PATH} {TARGET_PATH}. "
                    "It comes solely from the public issue: empty directory success, r.py import "
                    "side case, and actual analysis of each file. Do not assert internal module "
                    "names or rating text. Keep this first edit test-only. Baseline checks run "
                    "automatically; some controls should pass and at least one target must fail. "
                    "If source was edited first, revert only your source edit before establishing "
                    "the baseline. Preserve this target file exactly during source repair."
                )
        elif self.unresolved:
            priority = "repair_specific_failure"
            instruction = (
                "Use the exact failure below. Distinguish assertion failures "
                "from collection/environment errors. "
                "Make the smallest source correction for that evidence; do not broaden reading. "
                f"Only {max(0, self.config.repair_diagnostic_limit - self.diagnostics)} more "
                "unchanged diagnostic commands are allowed before an explicit incomplete stop. "
                "The same relevant tests will run after the edit. Keep edits and checks separate. "
                "For a reproduction, expose stdout/stderr and propagate its own exit code. "
                "Never hide output behind a redirect followed by set -e."
            )
        elif self.can_submit:
            priority = "submit_validated_patch"
            instruction = (
                "Target and selected public regressions passed for this diff, with no unresolved "
                'failure. Review and submit with {"tool":"submit"}; official grading is pending. '
                "Avoid unrelated exploration. Selected local tests do not prove all behavior."
            )
        elif self.patch.strip() and not self.plan.get("target"):
            priority = "add_target_regression"
            instruction = (
                "Add one focused public regression test for the issue's expected behavior. "
                "Preserve existing tests. Caller regressions do not substitute for the target. "
                "Edit alone; the policy runs each selected pytest command separately."
            )
        else:
            priority = "verify_selected_regressions"
            instruction = (
                "Use selected public tests and caller paths. Missing or failed checks block "
                "submission. Do not weaken assertions, suppress errors or make unrelated edits. "
                "Edit separately from testing; each test has its own output and exit code."
            )
        incomplete = self.diagnostics >= self.config.repair_diagnostic_limit and bool(
            self.unresolved
        )
        source_status = None
        if self.source:
            failures = [c.record() for c in self.unresolved.values()]
            source_status = self.source.status(self.patch, failures)
            incomplete = self.source.diagnostics > self.config.repair_diagnostic_limit and bool(
                failures
            )
            if failures and self.target_contract and self.target_contract.red_proven:
                priority = (
                    "repair_source_failure"
                    if production_patch(self.patch)
                    else "make_first_source_edit"
                )
                instruction = self.source.guidance(
                    self.patch, failures, detailed_retests=self.efficiency is None
                )
                if self.evidence:
                    instruction = self.evidence.guidance(
                        self.patch,
                        failures,
                        self.plan,
                        self.source.diagnostics,
                        self.config.repair_diagnostic_limit,
                    )
        if seconds_left <= 30 or actions_left <= 1:
            instruction += (
                " Resources are low; never declare success with missing or failed checks."
            )
        if self.efficiency and not self.evidence and not self.can_submit:
            instruction += self.efficiency.guidance(self.patch, unresolved=bool(self.unresolved))
        status = {
            "next_priority": priority,
            "next_action": instruction,
            "validation_ready_to_submit": self.can_submit,
            "regression_selection": self.plan,
            "verification_results": self.results,
            "unresolved_failures": [c.record() for c in self.unresolved.values()],
            "failure_excerpt": self.failure_output,
            "diagnostics_since_failure": self.source.diagnostics
            if self.source
            else self.diagnostics,
            "failure_repair_edits": len(self.source.failure_repairs)
            if self.source
            else self.failure_repairs,
            "incomplete_stop": incomplete,
        }
        if self.target_contract:
            status["target_contract"] = self.target_contract.status()
        if source_status is not None:
            status["source_decision"] = source_status
        if self.efficiency:
            status["execution_efficiency"] = self.efficiency.status(self.patch)
        if self.evidence and self.unresolved:
            status["repair_evidence"] = self.evidence.packet(
                self.patch, [c.record() for c in self.unresolved.values()], self.plan
            )
        return status
