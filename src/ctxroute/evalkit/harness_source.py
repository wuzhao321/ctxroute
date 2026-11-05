"""Validate harness source metadata and remove future Git history from task images."""

import re
from collections.abc import Mapping, Sequence
from typing import Protocol


class SourceSpec(Protocol):
    @property
    def instance_id(self) -> str: ...
    @property
    def repo(self) -> str: ...
    @property
    def arch(self) -> str: ...
    @property
    def repo_script_list(self) -> Sequence[str]: ...


def validated_base(
    spec: SourceSpec, metadata: Mapping[str, str], receipt: Mapping[str, str], *, arch: str
) -> str:
    """TestSpec stores the reset command, not a base_commit attribute."""
    base = metadata["base_commit"]
    if (
        re.fullmatch(r"[0-9a-f]{40}", base) is None
        or spec.arch != arch
        or base != receipt["base_commit"]
        or spec.instance_id != metadata["instance_id"]
        or spec.repo != metadata["repo"]
        or f"git reset --hard {base}" not in spec.repo_script_list
    ):
        raise ValueError("Official recipe does not match the pinned source")
    return base


def sanitization_commands(base: str) -> list[str]:
    """Permit an empty official setup commit only; reject source modifications."""
    if re.fullmatch(r"[0-9a-f]{40}", base) is None:
        raise ValueError("Expected an exact Git commit")
    return [
        f"git diff --quiet && git diff --cached --quiet && "
        f'if test "$(git rev-parse HEAD)" != "{base}"; then '
        f'test "$(git rev-parse HEAD^)" = "{base}" && '
        'test "$(git show -s --format=%s HEAD)" = "SWE-bench" && '
        f"git diff --quiet {base} HEAD && git reset --hard {base}; fi",
        f'test "$(git rev-parse HEAD)" = "{base}" && '
        'current="$(git symbolic-ref -q HEAD || true)" && '
        'git for-each-ref --format="%(refname)" | while IFS= read -r ref; do '
        'if [ "$ref" != "$current" ]; then git update-ref -d "$ref"; fi; done',
        "git reflog expire --expire=now --all && git gc --prune=now && "
        f'test "$(git rev-parse HEAD)" = "{base}" && '
        'test "$(git rev-list --all --not HEAD | wc -l)" -eq 0',
    ]
