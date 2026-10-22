import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from ctxroute.api_probe import Json, ProbeError, definitely_off_peak, normalize_usage, run_probe
from ctxroute.secrets import load_key, save_new_key
from ctxroute.tracing.budget import Ledger

DEMO_KEY = "unit-test-credential-never-real"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    repo = Path(__file__).resolve().parents[1]
    shutil.copytree(repo / "configs", tmp_path / "configs")
    shutil.copytree(repo / "data/prices", tmp_path / "data/prices")
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("a" * 40)
    save_new_key(tmp_path, DEMO_KEY)
    budget_path = tmp_path / "configs/budget.json"
    budget = json.loads(budget_path.read_text())
    budget["paid_execution_enabled"] = True
    budget_path.write_text(json.dumps(budget))
    return tmp_path


def off_peak() -> datetime:
    return datetime(2026, 10, 22, 12, tzinfo=UTC)


def response() -> Json:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": "OK"}}],
        "usage": {
            "prompt_tokens": 16,
            "completion_tokens": 2,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 16,
            "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def book(root: Path) -> Ledger:
    return Ledger(root / "results/budget/campaign.sqlite", hard_limit=D("10"), pause_at=D("8"))


def test_probe_success_accounts_for_usage_without_secrets(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    calls = 0

    def fake(payload: Json, key: str, timeout: int) -> Json:
        nonlocal calls
        calls += 1
        assert key == DEMO_KEY and timeout == 30
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 32 and payload["stream"] is False
        assert book(project).snapshot().reserved == D("0.10")
        return response()

    result = run_probe(project, execute=True, transport=fake, clock=off_peak)
    assert result["status"] == "passed" and calls == 1
    assert book(project).snapshot().spent == D("0.0000036")
    assert book(project).snapshot().reserved == 0
    assert DEMO_KEY not in capsys.readouterr().out
    for path in (project / "results/runs").rglob("*"):
        if path.is_file():
            assert DEMO_KEY not in path.read_text()


def test_dry_run_does_not_read_key_or_create_ledger(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    shutil.copytree(repo / "configs", tmp_path / "configs")
    shutil.copytree(repo / "data/prices", tmp_path / "data/prices")
    assert run_probe(tmp_path)["mode"] == "dry_run"
    assert not (tmp_path / "results").exists()
    with pytest.raises(ProbeError, match="disabled"):
        run_probe(tmp_path, execute=True)


def test_transport_failure_retains_reservation_and_never_retries(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    calls = 0

    def fail(payload: Json, key: str, timeout: int) -> Json:
        nonlocal calls
        calls += 1
        raise TimeoutError(key)

    with pytest.raises(ProbeError, match="transport_or_response_error"):
        run_probe(project, execute=True, transport=fail, clock=off_peak)
    assert calls == 1 and book(project).snapshot().reserved == D("0.10")
    assert DEMO_KEY not in next((project / "results/runs").glob("*/traces.jsonl")).read_text()


@pytest.mark.parametrize("field", ["prompt_cache_hit_tokens", "completion_tokens", "total_tokens"])
def test_missing_usage_is_not_zero(field: str) -> None:
    data = response()
    del data["usage"][field]
    with pytest.raises(ProbeError):
        normalize_usage(data)


def test_inconsistent_cache_fields_are_rejected() -> None:
    data = response()
    data["usage"]["prompt_tokens_details"]["cached_tokens"] = 10
    with pytest.raises(ProbeError, match="inconsistent_cache"):
        normalize_usage(data)


def test_invalid_usage_keeps_money_reserved(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ProbeError, match="missing_usage"):
        run_probe(project, execute=True, transport=lambda payload, key, timeout: {}, clock=off_peak)
    assert book(project).snapshot().reserved == D("0.10")


def test_rate_ambiguity_does_not_claim_invoice_cost(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = run_probe(
        project,
        execute=True,
        transport=lambda payload, key, timeout: response(),
        clock=lambda: datetime(2026, 10, 22, 2, tzinfo=UTC),
    )
    assert result["invoice_verified"] is False
    assert "usage_calculated_cost_usd" not in result
    assert book(project).snapshot().reserved == D("0.10")


def test_peak_boundary_and_weekend() -> None:
    windows = [["01:00", "04:00"], ["06:00", "10:00"]]
    assert not definitely_off_peak(
        datetime(2026, 10, 22, 0, 59, 59, tzinfo=UTC),
        datetime(2026, 10, 22, 1, tzinfo=UTC),
        windows,
    )
    assert definitely_off_peak(
        datetime(2026, 10, 24, 2, tzinfo=UTC), datetime(2026, 10, 24, 3, tzinfo=UTC), windows
    )


def test_usage_guard_blocks_further_calls(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    data = response()
    data["usage"].update(prompt_tokens=5000, prompt_cache_miss_tokens=5000, total_tokens=5002)
    with pytest.raises(ProbeError, match="exceeds_probe_guard"):
        run_probe(
            project, execute=True, transport=lambda payload, key, timeout: data, clock=off_peak
        )
    assert book(project).snapshot().blocked


def test_key_file_permissions_no_overwrite_and_env_precedence(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".env\n")
    save_new_key(tmp_path, DEMO_KEY)
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert load_key(tmp_path, {}) == DEMO_KEY
    with pytest.raises(FileExistsError):
        save_new_key(tmp_path, "replacement")
    assert load_key(tmp_path, {"DEEPSEEK_API_KEY": "from-env"}) == "from-env"
    (tmp_path / ".env").chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_key(tmp_path, {})


def test_repriced_request_cannot_bypass_preflight_reservation(project: Path) -> None:
    path = project / "data/prices/2026-10-22.json"
    prices = json.loads(path.read_text())
    prices["models"]["cheap"]["peak"]["uncached_input"] = 1000
    path.write_text(json.dumps(prices))
    with pytest.raises(ProbeError, match="configured_guard_exceeds"):
        run_probe(project, execute=True)
    assert not (project / "results").exists()
