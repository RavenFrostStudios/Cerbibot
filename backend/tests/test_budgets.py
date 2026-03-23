import json

import pytest

from orchestrator.budgets import BudgetExceededError, BudgetTracker
from orchestrator.config import BudgetConfig


def test_budget_tracker_records_and_persists(tmp_path) -> None:
    usage_file = tmp_path / "usage.json"
    tracker = BudgetTracker(
        BudgetConfig(
            session_usd_cap=1.0,
            daily_usd_cap=2.0,
            monthly_usd_cap=5.0,
            usage_file=str(usage_file),
        )
    )
    tracker.record_cost("openai", 0.2, tokens_in=100, tokens_out=50)

    loaded = json.loads(usage_file.read_text())
    assert "daily_totals" in loaded
    assert "monthly_totals" in loaded
    assert int(loaded["daily_totals"].get("requests", 0)) == 1
    assert int(loaded["monthly_totals"].get("requests", 0)) == 1
    assert int(loaded["daily_totals"]["providers"]["openai"].get("requests", 0)) == 1
    assert int(loaded["monthly_totals"]["providers"]["openai"].get("requests", 0)) == 1
    assert tracker.state().session_spend == pytest.approx(0.2)


def test_budget_tracker_enforces_caps(tmp_path) -> None:
    tracker = BudgetTracker(
        BudgetConfig(
            session_usd_cap=0.1,
            daily_usd_cap=0.1,
            monthly_usd_cap=0.1,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    with pytest.raises(BudgetExceededError):
        tracker.check_would_fit(0.11)


def test_budget_rollover_resets_daily_and_monthly(tmp_path) -> None:
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(
        json.dumps(
            {
                "last_reset_date": "2025-01-01",
                "last_reset_month": "2025-01",
                "daily_totals": {
                    "cost": 1.0,
                    "requests": 3,
                    "providers": {"openai": {"cost": 1.0, "tokens_in": 1, "tokens_out": 1, "requests": 3}},
                },
                "monthly_totals": {
                    "cost": 2.0,
                    "requests": 5,
                    "providers": {"openai": {"cost": 2.0, "tokens_in": 2, "tokens_out": 2, "requests": 5}},
                },
                "history": {"daily": {}, "monthly": {}},
            }
        ),
        encoding="utf-8",
    )

    tracker = BudgetTracker(
        BudgetConfig(
            session_usd_cap=10.0,
            daily_usd_cap=10.0,
            monthly_usd_cap=10.0,
            usage_file=str(usage_file),
        )
    )
    state = tracker.state()
    assert state.daily_spend == pytest.approx(0.0)
    assert state.monthly_spend == pytest.approx(0.0)
    totals = tracker.usage_totals()
    assert int(totals["daily_totals"].get("requests", 0)) == 0
    assert int(totals["monthly_totals"].get("requests", 0)) == 0
