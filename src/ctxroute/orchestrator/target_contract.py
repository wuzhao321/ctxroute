"""Public target assertions must fail before implementation edits and survive the fix."""

import hashlib
import json
import re
from typing import Any

from ctxroute.domain import Observation


class TargetContract:
    def __init__(self, *, allow_controls: bool = False) -> None:
        self.fingerprint = ""
        self.selectors: set[str] = set()
        self.failed_on_base: set[str] = set()
        self.base_failures: list[dict[str, Any]] = []
        self.current_matches = False
        self.implementation_changed = False
        self.baseline_candidate = False
        self.allow_controls = allow_controls
        self.observed_on_base: set[str] = set()

    def select(self, plan: dict[str, Any]) -> None:
        info = plan.get("target_contract", {})
        self.implementation_changed = info.get("implementation_changed", True)
        selectors = set(plan.get("target", []))
        valid = bool(info.get("test_only_valid"))
        fingerprint = hashlib.sha256(
            json.dumps(info.get("file_sha256", {}), sort_keys=True).encode()
        ).hexdigest()
        self.baseline_candidate = valid and not self.implementation_changed
        if self.baseline_candidate and not self.base_failures:
            self.fingerprint, self.selectors = fingerprint, selectors
            self.observed_on_base.clear()
        self.current_matches = (
            bool(self.fingerprint)
            and fingerprint == self.fingerprint
            and selectors == self.selectors
        )

    @property
    def red_proven(self) -> bool:
        if self.allow_controls:
            return (
                bool(self.failed_on_base)
                and self.selectors <= self.observed_on_base
                and self.current_matches
            )
        return (
            bool(self.selectors) and self.selectors <= self.failed_on_base and self.current_matches
        )

    def observe(
        self, selectors: tuple[str, ...], obs: Observation, before: str, after: str, step: int
    ) -> None:
        # Each target node runs independently during baseline establishment.
        # Setup/import errors or a green/vacuous test cannot establish the defect.
        if (
            self.baseline_candidate
            and self.current_matches
            and before == after
            and not obs.timed_out
            and not obs.output_limited
            and obs.exit_code == 0
            and re.search(r"\b[1-9]\d* passed\b", obs.output)
        ):
            self.observed_on_base.update(selectors)
        if (
            self.baseline_candidate
            and self.current_matches
            and before == after
            and obs.exit_code == 1
            and not obs.timed_out
            and not obs.output_limited
            and re.search(r"\b[1-9]\d* failed\b", obs.output)
            and "AssertionError" in obs.output
            and not re.search(r"\b(?:[1-9]\d* errors?|ERROR collecting)\b", obs.output)
        ):
            self.failed_on_base.update(selectors)
            self.observed_on_base.update(selectors)
            self.base_failures.append(
                {
                    "step": step,
                    "selectors": list(selectors),
                    "exit_code": obs.exit_code,
                    "patch_sha256": hashlib.sha256(after.encode()).hexdigest(),
                    "target_fingerprint": self.fingerprint,
                }
            )

    def status(self) -> dict[str, Any]:
        return {
            "baseline_assertion_proven": self.red_proven,
            "target_unchanged": self.current_matches,
            "implementation_changed": self.implementation_changed,
            "target_fingerprint": self.fingerprint,
            "baseline_failures": self.base_failures,
            "baseline_observed_selectors": sorted(self.observed_on_base),
        }
