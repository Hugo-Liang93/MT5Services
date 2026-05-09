from __future__ import annotations

from datetime import datetime, timezone

from src.trading.recovery.risk_budget import (
    RecoveryRiskBudgetGuard,
    RecoveryRiskBudgetSettings,
)


class DummyCycleStateStore:
    def __init__(self, rows):
        self.rows = list(rows)

    def list_recovery_cycle_states(self, **kwargs):
        return list(self.rows)


def test_risk_budget_ignores_losses_from_other_account_key() -> None:
    guard = RecoveryRiskBudgetGuard()
    store = DummyCycleStateStore(
        [
            {
                "cycle_id": "other-account-loss",
                "account_key": "demo:broker:other",
                "symbol": "XAUUSD",
                "strategy": "tick_martingale_probe",
                "status_reason": "resident_recovery_cycle_loss_limit_submitted",
                "realized_pnl": -12.5,
                "closed_at": datetime(2026, 5, 8, 12, tzinfo=timezone.utc).isoformat(),
                "metadata": {"initial_result": {"dry_run": False}},
            }
        ]
    )

    decision = guard.assess(
        settings=RecoveryRiskBudgetSettings(
            max_daily_recovery_loss_amount=5.0,
        ),
        state_store=store,
        now=datetime(2026, 5, 8, 13, tzinfo=timezone.utc),
        account_key="demo:broker:current",
        symbol="XAUUSD",
        strategy="tick_martingale_probe",
        dry_run=False,
    )

    assert decision.allowed is True
    assert decision.reason == "recovery_risk_budget_ok"
    assert decision.snapshot["account_key"] == "demo:broker:current"
    assert decision.snapshot["daily_realized_loss_amount"] == 0.0
    assert decision.snapshot["remaining_daily_loss_amount"] == 5.0
