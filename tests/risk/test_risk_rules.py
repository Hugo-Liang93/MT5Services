"""Tests for MarginAvailabilityRule and TradeFrequencyRule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from src.config import EconomicConfig
from src.config.models.runtime import RiskConfig
from src.risk.models import TradeIntent
from src.risk.rules import (
    AccountSnapshotRule,
    DailyLossLimitRule,
    MarginAvailabilityRule,
    RuleContext,
    TradeFrequencyRule,
)
from src.risk.service import resolve_risk_failure_key


@dataclass
class FakeAccountInfo:
    balance: float
    equity: float
    margin: float
    margin_free: float | None
    profit: float
    leverage: int
    currency: str
    day_start_balance: float | None = None
    daily_realized_pnl: float | None = None
    daily_pnl: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAccountProvider:
    def __init__(
        self, *, margin_free: Optional[float] = 5000.0, balance: float = 10000.0
    ):
        self._margin_free = margin_free
        self._balance = balance
        self._info = FakeAccountInfo(
            balance=self._balance,
            equity=self._balance,
            margin=0.0,
            margin_free=self._margin_free,
            profit=0.0,
            leverage=100,
            currency="USD",
        )

    def account_info(self):
        return self._info

    def positions(self, symbol=None):
        return []

    def orders(self, symbol=None):
        return []


class BrokenAccountProvider:
    def account_info(self):
        raise RuntimeError("account unavailable")

    def positions(self, symbol=None):
        return []

    def orders(self, symbol=None):
        return []


class FullyBrokenAccountProvider:
    def account_info(self):
        raise RuntimeError("account unavailable")

    def positions(self, symbol=None):
        raise RuntimeError("positions unavailable")

    def orders(self, symbol=None):
        raise RuntimeError("orders unavailable")


class FakeTradeFrequencyProvider:
    def __init__(self, *, count: int = 0, error: Exception | None = None) -> None:
        self.count = count
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def count_trades_since(
        self, since: datetime, *, account_key: str | None = None
    ) -> int:
        self.calls.append({"since": since, "account_key": account_key})
        if self.error is not None:
            raise self.error
        return self.count


def _ctx(
    *,
    metadata: Optional[Dict[str, Any]] = None,
    margin_safety_factor: float = 1.2,
    margin_free: Optional[float] = 5000.0,
    account_provider: Any = None,
    max_trades_per_day: Optional[int] = None,
    max_trades_per_hour: Optional[int] = None,
    data_unavailable_policy: str = "warn_only",
    trade_frequency_provider: Any = None,
    account_key: str | None = None,
    at_time: datetime | None = None,
) -> RuleContext:
    intent = TradeIntent(
        symbol="XAUUSD",
        volume=0.1,
        side="buy",
        metadata=dict(metadata or {}),
        at_time=at_time,
    )
    risk = RiskConfig(
        enabled=True,
        margin_safety_factor=margin_safety_factor,
        max_trades_per_day=max_trades_per_day,
        max_trades_per_hour=max_trades_per_hour,
        data_unavailable_policy=data_unavailable_policy,
    )
    if account_provider is None:
        account_provider = FakeAccountProvider(margin_free=margin_free)
    return RuleContext(
        intent=intent,
        economic_settings=EconomicConfig(),
        risk_settings=risk,
        account_provider=account_provider,
        trade_frequency_provider=trade_frequency_provider,
        account_key=account_key,
    )


# ---------------------------------------------------------------------------
# AccountSnapshotRule / DailyLossLimitRule
# ---------------------------------------------------------------------------


class TestRiskDataAvailabilityPolicy:
    def test_account_snapshot_blocks_when_account_state_unavailable_fail_closed(self):
        ctx = _ctx(
            account_provider=FullyBrokenAccountProvider(),
            data_unavailable_policy="fail_closed",
        )
        ctx.risk_settings = RiskConfig(
            enabled=True,
            max_positions_per_symbol=1,
            data_unavailable_policy="fail_closed",
        )

        checks = AccountSnapshotRule().evaluate(ctx)

        assert len(checks) == 1
        assert checks[0].name == "account_snapshot"
        assert checks[0].verdict == "block"
        assert checks[0].reason == "risk_data_unavailable"

    def test_daily_loss_blocks_when_account_info_unavailable_fail_closed(self):
        ctx = _ctx(
            account_provider=BrokenAccountProvider(),
            data_unavailable_policy="fail_closed",
        )
        ctx.risk_settings = RiskConfig(
            enabled=True,
            daily_loss_limit_pct=3.0,
            data_unavailable_policy="fail_closed",
        )

        checks = DailyLossLimitRule().evaluate(ctx)

        assert len(checks) == 1
        assert checks[0].name == "daily_loss_limit"
        assert checks[0].verdict == "block"
        assert checks[0].reason == "risk_data_unavailable"


# ---------------------------------------------------------------------------
# MarginAvailabilityRule
# ---------------------------------------------------------------------------


class TestMarginAvailabilityRule:
    rule = MarginAvailabilityRule()

    def test_passes_when_free_margin_sufficient(self):
        ctx = _ctx(metadata={"estimated_margin": 100.0}, margin_free=5000.0)
        checks = self.rule.evaluate(ctx)
        assert checks == []

    def test_blocks_when_free_margin_insufficient(self):
        # 100 * 1.2 = 120 > 100 free margin → block
        ctx = _ctx(metadata={"estimated_margin": 100.0}, margin_free=100.0)
        checks = self.rule.evaluate(ctx)
        assert len(checks) == 1
        assert checks[0].verdict == "block"
        assert "Insufficient free margin" in checks[0].reason
        assert checks[0].details["shortfall"] > 0

    def test_passes_when_margin_exactly_at_threshold(self):
        # 100 * 1.2 = 120 == 120 → passes
        ctx = _ctx(metadata={"estimated_margin": 100.0}, margin_free=120.0)
        checks = self.rule.evaluate(ctx)
        assert checks == []

    def test_skipped_when_safety_factor_zero(self):
        ctx = _ctx(
            metadata={"estimated_margin": 100.0},
            margin_free=10.0,
            margin_safety_factor=0.0,
        )
        checks = self.rule.evaluate(ctx)
        assert checks == []

    def test_skipped_when_no_estimated_margin(self):
        ctx = _ctx(metadata={}, margin_free=100.0)
        checks = self.rule.evaluate(ctx)
        assert checks == []

    def test_skipped_when_risk_disabled(self):
        ctx = _ctx(metadata={"estimated_margin": 100.0}, margin_free=10.0)
        ctx.risk_settings = RiskConfig(enabled=False)
        checks = self.rule.evaluate(ctx)
        assert checks == []

    def test_warns_when_account_unavailable(self):
        ctx = _ctx(
            metadata={"estimated_margin": 100.0},
            account_provider=BrokenAccountProvider(),
        )
        checks = self.rule.evaluate(ctx)
        assert len(checks) == 1
        assert checks[0].verdict == "warn"

    def test_blocks_when_account_unavailable_fail_closed(self):
        ctx = _ctx(
            metadata={"estimated_margin": 100.0},
            account_provider=BrokenAccountProvider(),
            data_unavailable_policy="fail_closed",
        )
        checks = self.rule.evaluate(ctx)
        assert len(checks) == 1
        assert checks[0].verdict == "block"
        assert checks[0].reason == "risk_data_unavailable"

    def test_blocks_when_margin_estimate_missing_fail_closed(self):
        ctx = _ctx(
            metadata={},
            margin_free=100.0,
            data_unavailable_policy="fail_closed",
        )
        checks = self.rule.evaluate(ctx)
        assert len(checks) == 1
        assert checks[0].verdict == "block"
        assert checks[0].reason == "risk_data_unavailable"

    def test_warns_when_free_margin_field_missing(self):
        provider = FakeAccountProvider(margin_free=None)
        ctx = _ctx(
            metadata={"estimated_margin": 100.0},
            account_provider=provider,
        )
        # margin_free is None in account_info → warn
        checks = self.rule.evaluate(ctx)
        assert len(checks) == 1
        assert checks[0].verdict == "warn"

    def test_custom_safety_factor(self):
        # 100 * 2.0 = 200 > 150 → block
        ctx = _ctx(
            metadata={"estimated_margin": 100.0},
            margin_free=150.0,
            margin_safety_factor=2.0,
        )
        checks = self.rule.evaluate(ctx)
        assert len(checks) == 1
        assert checks[0].verdict == "block"

    def test_skipped_when_no_account_provider(self):
        intent = TradeIntent(
            symbol="XAUUSD",
            volume=0.1,
            side="buy",
            metadata={"estimated_margin": 100.0},
        )
        ctx = RuleContext(
            intent=intent,
            economic_settings=EconomicConfig(),
            risk_settings=RiskConfig(enabled=True),
            account_provider=None,
        )
        checks = self.rule.evaluate(ctx)
        assert checks == []


# ---------------------------------------------------------------------------
# TradeFrequencyRule
# ---------------------------------------------------------------------------


class TestTradeFrequencyRule:
    @staticmethod
    def _anchor_now() -> datetime:
        return datetime.now(timezone.utc).replace(
            hour=12, minute=0, second=0, microsecond=0
        )

    def test_passes_when_no_limits(self):
        rule = TradeFrequencyRule()
        rule.record_trade()
        ctx = _ctx(max_trades_per_day=None, max_trades_per_hour=None)
        checks = rule.evaluate(ctx)
        assert checks == []

    def test_blocks_when_daily_limit_reached(self):
        rule = TradeFrequencyRule()
        now = self._anchor_now()
        for i in range(10):
            rule.record_trade(now - timedelta(minutes=i))
        ctx = _ctx(max_trades_per_day=10, at_time=now)
        checks = rule.evaluate(ctx)
        assert any(c.name == "max_trades_per_day" for c in checks)
        assert checks[0].verdict == "block"

    def test_passes_when_under_daily_limit(self):
        rule = TradeFrequencyRule()
        now = self._anchor_now()
        for i in range(5):
            rule.record_trade(now - timedelta(minutes=i))
        ctx = _ctx(max_trades_per_day=10, at_time=now)
        checks = rule.evaluate(ctx)
        assert checks == []

    def test_blocks_when_hourly_limit_reached(self):
        rule = TradeFrequencyRule()
        now = datetime.now(timezone.utc)
        for i in range(5):
            rule.record_trade(now - timedelta(minutes=i * 5))
        ctx = _ctx(max_trades_per_hour=5)
        checks = rule.evaluate(ctx)
        assert any(c.name == "max_trades_per_hour" for c in checks)

    def test_passes_when_under_hourly_limit(self):
        rule = TradeFrequencyRule()
        now = datetime.now(timezone.utc)
        for i in range(3):
            rule.record_trade(now - timedelta(minutes=i))
        ctx = _ctx(max_trades_per_hour=5)
        checks = rule.evaluate(ctx)
        assert checks == []

    def test_old_trades_not_counted_hourly(self):
        rule = TradeFrequencyRule()
        now = datetime.now(timezone.utc)
        # All trades older than 1 hour
        for i in range(10):
            rule.record_trade(now - timedelta(hours=2, minutes=i))
        ctx = _ctx(max_trades_per_hour=5)
        checks = rule.evaluate(ctx)
        assert checks == []

    def test_old_trades_not_counted_daily(self):
        rule = TradeFrequencyRule()
        now = self._anchor_now()
        yesterday = now - timedelta(days=1)
        for i in range(20):
            rule.record_trade(yesterday - timedelta(minutes=i))
        ctx = _ctx(max_trades_per_day=10, at_time=now)
        checks = rule.evaluate(ctx)
        assert checks == []

    def test_both_limits_can_block_simultaneously(self):
        rule = TradeFrequencyRule()
        now = self._anchor_now()
        for i in range(10):
            rule.record_trade(now - timedelta(minutes=i))
        ctx = _ctx(max_trades_per_day=10, max_trades_per_hour=5, at_time=now)
        checks = rule.evaluate(ctx)
        assert len(checks) == 2
        names = {c.name for c in checks}
        assert "max_trades_per_day" in names
        assert "max_trades_per_hour" in names

    def test_prunes_old_entries(self):
        rule = TradeFrequencyRule()
        # Directly inject timestamps to bypass per-call pruning
        now = datetime.now(timezone.utc)
        rule._trade_timestamps = [
            now - timedelta(hours=50),  # older than 48h → should be pruned
            now - timedelta(hours=1),  # recent → should survive
        ]
        assert len(rule._trade_timestamps) == 2
        # Recording a new trade triggers pruning of >48h entries
        rule.record_trade()
        assert (
            len(rule._trade_timestamps) == 2
        )  # old one pruned, 2 remain (recent + new)

    def test_skipped_when_risk_disabled(self):
        rule = TradeFrequencyRule()
        for _ in range(20):
            rule.record_trade()
        ctx = _ctx(max_trades_per_day=5)
        ctx.risk_settings = RiskConfig(enabled=False)
        checks = rule.evaluate(ctx)
        assert checks == []

    def test_uses_persistent_account_scoped_frequency_provider(self):
        provider = FakeTradeFrequencyProvider(count=3)
        ctx = _ctx(
            max_trades_per_day=3,
            trade_frequency_provider=provider,
            account_key="live:broker:123",
        )

        checks = TradeFrequencyRule().evaluate(ctx)

        assert any(c.name == "max_trades_per_day" for c in checks)
        assert provider.calls
        assert provider.calls[0]["account_key"] == "live:broker:123"

    def test_provider_failure_follows_risk_data_policy(self):
        provider = FakeTradeFrequencyProvider(error=RuntimeError("db unavailable"))
        ctx = _ctx(
            max_trades_per_day=3,
            data_unavailable_policy="fail_closed",
            trade_frequency_provider=provider,
            account_key="live:broker:123",
        )

        checks = TradeFrequencyRule().evaluate(ctx)

        assert len(checks) == 1
        assert checks[0].name == "trade_frequency"
        assert checks[0].verdict == "block"
        assert checks[0].reason == "risk_data_unavailable"


class TestResolveRiskFailureKey:
    def test_prefers_block_check_over_warning(self):
        assert (
            resolve_risk_failure_key(
                {
                    "checks": [
                        {"name": "session_window", "verdict": "warn"},
                        {"name": "margin_availability", "verdict": "block"},
                    ]
                }
            )
            == "margin_availability"
        )

    def test_fallback_to_first_available_check_name(self):
        assert (
            resolve_risk_failure_key(
                {"checks": [{"name": "session_window", "verdict": "warn"}]}
            )
            == "session_window"
        )
