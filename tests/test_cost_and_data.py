import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from ctxroute.evalkit.split import agent_input, partition
from ctxroute.tracing.cost import Price, Usage


def test_cost_includes_cached_uncached_and_output() -> None:
    price = Price(D("0.006"), D("0.3"), D("1.2"))
    assert price.cost(Usage(1_000_000, 400_000, 100_000)) == D("0.3024")
    assert price.cost(Usage(0, 0, 0)) == 0


@pytest.mark.parametrize("counts", [(2, 3, 1), (-1, 0, 0), (1, 0, -1), (True, 0, 0)])
def test_bad_usage_is_rejected(counts: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        Usage(*counts)


def test_agent_input_excludes_answers_and_hints() -> None:
    safe = {
        "instance_id": "example__repo-1",
        "repo": "example/repo",
        "base_commit": "abc",
        "problem_statement": "Fix the issue",
    }
    assert (
        agent_input({**safe, "patch": "SECRET", "test_patch": "SECRET", "hints_text": "X"}) == safe
    )
    with pytest.raises(ValueError):
        agent_input({"instance_id": "x", "patch": "SECRET"})


def test_partition_is_disjoint_complete_and_independent_of_input_order() -> None:
    ids = [f"repo__task-{i}" for i in range(300)]
    dev, test = partition(ids, "20261022")
    assert len(dev) == 60 and len(test) == 240
    assert not set(dev) & set(test)
    assert set(dev + test) == set(ids)
    assert partition(list(reversed(ids)), "20261022") == (dev, test)
    assert partition(ids, "another-seed") != (dev, test)
    with pytest.raises(ValueError):
        partition(ids[:-1] + [ids[0]], "20261022")


def test_frozen_split_reproduces_without_network() -> None:
    path = Path(__file__).resolve().parents[1] / "data/split.json"
    saved = json.loads(path.read_text())
    assert partition(saved["dev"] + saved["test"], saved["seed"]) == (saved["dev"], saved["test"])
    assert saved["pilot_candidate_order"] == saved["dev"][:10]
    assert saved["smoke_instance"] == saved["dev"][0]
    assert saved["formal_test_enabled"] is False
