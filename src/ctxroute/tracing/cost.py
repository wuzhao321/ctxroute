"""Exact decimal accounting; unknown usage must remain unsettled."""

from dataclasses import dataclass
from decimal import Decimal


def money(value: str | Decimal) -> Decimal:
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0:
        raise ValueError("Amount must be finite and nonnegative")
    return amount


@dataclass(frozen=True)
class Usage:
    """Normalized usage: input_tokens includes cached input tokens."""

    input_tokens: int
    cached_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.cached_tokens, self.output_tokens):
            if type(value) is not int or value < 0:
                raise ValueError("Usage must contain nonnegative integer counts")
        if self.cached_tokens > self.input_tokens:
            raise ValueError("Cached tokens exceed total input")


@dataclass(frozen=True)
class Price:
    """USD per million tokens for one model and one pricing period."""

    cached_input: Decimal
    uncached_input: Decimal
    output: Decimal

    def __post_init__(self) -> None:
        for rate in (self.cached_input, self.uncached_input, self.output):
            money(rate)

    def cost(self, usage: Usage) -> Decimal:
        return (
            usage.cached_tokens * self.cached_input
            + (usage.input_tokens - usage.cached_tokens) * self.uncached_input
            + usage.output_tokens * self.output
        ) / Decimal(1_000_000)
