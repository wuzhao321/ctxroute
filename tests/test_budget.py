from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path

import pytest

from ctxroute.tracing.budget import BudgetStopped, Ledger


def ledger(path: Path, enabled: bool = True) -> Ledger:
    return Ledger(path, hard_limit=D("10"), pause_at=D("8"), enabled=enabled)


def test_disabled_and_threshold(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    with pytest.raises(BudgetStopped, match="disabled"):
        ledger(path, False).reserve("disabled", D("0.1"), "model_api")
    book = ledger(path)
    book.reserve("cloud", D("6"), "cloud_compute")
    with pytest.raises(BudgetStopped, match="80%"):
        book.reserve("api", D("2"), "model_api")
    with pytest.raises(BudgetStopped, match="hard limit"):
        book.reserve("large", D("5"), "model_api")
    assert book.snapshot().reserved == D("6")


def test_unknown_charge_survives_restart_and_duplicate_is_blocked(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    book = ledger(path)
    book.reserve("request", D("0.5"), "model_api")
    book.settle("request", None)
    restarted = ledger(path)
    assert restarted.snapshot().reserved == D("0.5")
    with pytest.raises(BudgetStopped, match="already"):
        restarted.reserve("request", D("0.5"), "model_api")
    restarted.settle("request", D("0.03"))
    restarted.settle("request", D("0.03"))
    assert restarted.snapshot().spent == D("0.03")
    assert restarted.snapshot().reserved == 0
    with pytest.raises(ValueError, match="overwritten"):
        restarted.settle("request", D("0"))


def test_overrun_is_recorded_before_stop(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    book = ledger(path)
    book.reserve("bad-bound", D("0.1"), "model_api")
    with pytest.raises(BudgetStopped, match="recorded"):
        book.settle("bad-bound", D("0.2"))
    assert ledger(path).snapshot().spent == D("0.2")
    with pytest.raises(BudgetStopped, match="Blocked"):
        ledger(path).reserve("next", D("0.1"), "model_api")


def test_concurrent_reservations_cannot_oversubscribe(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    ledger(path)

    def reserve(index: int) -> bool:
        try:
            ledger(path).reserve(f"r{index}", D("1"), "model_api")
            return True
        except BudgetStopped:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(20))) == 7
    assert ledger(path).snapshot().reserved == 7


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "-Infinity", "0"])
def test_invalid_reservations(tmp_path: Path, amount: str) -> None:
    with pytest.raises(ValueError):
        ledger(tmp_path / "budget.sqlite").reserve("bad", D(amount), "model_api")


def test_cannot_silently_change_limits(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    ledger(path)
    with pytest.raises(ValueError, match="limits differ"):
        Ledger(path, hard_limit=D("20"), pause_at=D("16"))
