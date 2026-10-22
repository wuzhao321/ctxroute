"""Stable selection uses identifiers only, never outcomes or task difficulty."""

import hashlib
from collections.abc import Mapping, Sequence

AGENT_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement")


def agent_input(row: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in AGENT_FIELDS:
        value = row.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Missing or invalid permitted field: {key}")
        result[key] = value
    return result


def partition(ids: Sequence[str], seed: str, dev_size: int = 60) -> tuple[list[str], list[str]]:
    if len(ids) != 300 or len(set(ids)) != 300:
        raise ValueError("Expected exactly 300 unique SWE-bench Lite test identifiers")
    if not 0 < dev_size < len(ids) or not seed:
        raise ValueError("Invalid split configuration")
    ranked = sorted(
        ids, key=lambda item: (hashlib.sha256(f"{seed}:{item}".encode()).hexdigest(), item)
    )
    return ranked[:dev_size], ranked[dev_size:]
