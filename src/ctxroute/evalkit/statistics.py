"""Instance-level paired statistics using deterministic resampling."""

import math
import random
import statistics
from collections.abc import Callable, Sequence
from typing import Any


def percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    if not 0 <= probability <= 1:
        raise ValueError("Probability outside [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def bootstrap(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = statistics.mean,
    *,
    seed: int = 20261022,
    draws: int = 10_000,
) -> dict[str, Any]:
    if not values:
        return {"n": 0, "estimate": None, "ci95": None}
    estimate = statistic(values)
    if len(values) < 2:
        return {"n": len(values), "estimate": estimate, "ci95": None}
    if draws < 1:
        raise ValueError("Positive resampling count required")
    rng = random.Random(seed)
    samples = [statistic(rng.choices(values, k=len(values))) for _ in range(draws)]
    return {
        "n": len(values),
        "estimate": estimate,
        "ci95": [percentile(samples, 0.025), percentile(samples, 0.975)],
        "seed": seed,
        "draws": draws,
    }


def mcnemar(pairs: Sequence[tuple[bool, bool]]) -> dict[str, int | float]:
    improved = sum(not baseline and candidate for baseline, candidate in pairs)
    regressed = sum(baseline and not candidate for baseline, candidate in pairs)
    discordant = improved + regressed
    probability = (
        min(
            1.0,
            2
            * sum(math.comb(discordant, k) for k in range(min(improved, regressed) + 1))
            / 2**discordant,
        )
        if discordant
        else 1.0
    )
    return {"improved": improved, "regressed": regressed, "p_two_sided": probability}
