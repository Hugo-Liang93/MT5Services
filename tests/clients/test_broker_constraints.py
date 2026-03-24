"""Broker constraint validation tests.

Covers:
- trade_mode: DISABLED / CLOSEONLY / LONGONLY / SHORTONLY / FULL
- stops_level: SL/TP minimum distance from market price
- freeze_level: warning-only informational check
- Integration with validate_trade_request() (raises on failure)
- Integration with TradingService.precheck_trade() (structured results)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import pytest

from src.clients.mt5_trading import MT5TradingClient, MT5TradingClientError
from src.trading.trading_service import TradingService


# ---------------------------------------------------------------------------
# Helpers: mock MT5 symbol_info / symbol_info_tick
# ---------------------------------------------------------------------------

def _make_symbol_info(
    *,
    trade_mode: int = 4,  # FULL
    trade_stops_level: int = 0,
    trade_freeze_level: int = 0,
    point: float = 0.01,
    digits: int = 2,
    volume_min: float = 0.01,
    volume_max: float = 100.0,
    volume_step: float = 0.01,
    filling_mode: int = 2,
) -> SimpleNamespace:
    return SimpleNamespace(
        name="XAUUSD",
        trade_mode=trade_mode,
        trade_stops_level=trade_stops_level,
        trade_freeze_level=trade_freeze_level,
        point=point,
        digits=digits,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        filling_mode=filling_mode,
        description="Gold",
        trade_contract_size=100.0,
        margin_initial=0.0,
        margin_maintenance=0.0,
        trade_tick_value=1.0,
        trade_tick_size=0.01,
    )


def _make_tick(bid: float = 2000.0, ask: float = 2000.50) -> SimpleNamespace:
    return SimpleNamespace(bid=bid, ask=ask)


# ---------------------------------------------------------------------------
# Unit tests: check_broker_constraints()
# ---------------------------------------------------------------------------

class TestCheckBrokerConstraints:
    """Direct tests for MT5TradingClient.check_broker_constraints()."""

    def _call(self, *, symbol_info, tick=None, **kwargs) -> list[dict[str, Any]]:
        client = MT5TradingClient.__new__(MT5TradingClient)
        with patch("src.clients.mt5_trading.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = symbol_info
            mock_mt5.symbol_info_tick.return_value = tick or _make_tick()
            # Stub connect to no-op
            client.connect = lambda: None
            return client.check_broker_constraints(**kwargs)

    # -- trade_mode tests --------------------------------------------------

    def test_trade_mode_full_passes(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_mode=4),
            symbol="XAUUSD", side="buy",
        )
        mode_check = next(c for c in checks if c["name"] == "trade_mode")
        assert mode_check["passed"] is True

    def test_trade_mode_disabled_blocks(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_mode=0),
            symbol="XAUUSD", side="buy",
        )
        mode_check = next(c for c in checks if c["name"] == "trade_mode")
        assert mode_check["passed"] is False
        assert "DISABLED" in mode_check["message"]

    def test_trade_mode_closeonly_blocks(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_mode=3),
            symbol="XAUUSD", side="sell",
        )
        mode_check = next(c for c in checks if c["name"] == "trade_mode")
        assert mode_check["passed"] is False
        assert "CLOSEONLY" in mode_check["message"]

    def test_trade_mode_longonly_blocks_sell(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_mode=1),
            symbol="XAUUSD", side="sell",
        )
        mode_check = next(c for c in checks if c["name"] == "trade_mode")
        assert mode_check["passed"] is False
        assert "LONGONLY" in mode_check["message"]

    def test_trade_mode_longonly_allows_buy(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_mode=1),
            symbol="XAUUSD", side="buy",
        )
        mode_check = next(c for c in checks if c["name"] == "trade_mode")
        assert mode_check["passed"] is True

    def test_trade_mode_shortonly_blocks_buy(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_mode=2),
            symbol="XAUUSD", side="buy",
        )
        mode_check = next(c for c in checks if c["name"] == "trade_mode")
        assert mode_check["passed"] is False
        assert "SHORTONLY" in mode_check["message"]

    def test_trade_mode_shortonly_allows_sell(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_mode=2),
            symbol="XAUUSD", side="sell",
        )
        mode_check = next(c for c in checks if c["name"] == "trade_mode")
        assert mode_check["passed"] is True

    # -- symbol not found --------------------------------------------------

    def test_symbol_not_found(self):
        checks = self._call(
            symbol_info=None,
            symbol="INVALID", side="buy",
        )
        assert len(checks) == 1
        assert checks[0]["name"] == "symbol_available"
        assert checks[0]["passed"] is False

    # -- stops_level tests -------------------------------------------------

    def test_stops_level_sl_too_close(self):
        # stops_level=50 points, point=0.01 → min distance = 0.50
        # buy at ask=2000.50, SL at 2000.20 → distance=0.30 < 0.50
        checks = self._call(
            symbol_info=_make_symbol_info(trade_stops_level=50, point=0.01),
            tick=_make_tick(bid=2000.0, ask=2000.50),
            symbol="XAUUSD", side="buy", sl=2000.20,
        )
        sl_check = next(c for c in checks if c["name"] == "stops_level_sl")
        assert sl_check["passed"] is False
        assert "SL too close" in sl_check["message"]

    def test_stops_level_sl_sufficient(self):
        # stops_level=50, point=0.01 → min=0.50
        # buy at ask=2000.50, SL at 1999.80 → distance=0.70 > 0.50
        checks = self._call(
            symbol_info=_make_symbol_info(trade_stops_level=50, point=0.01),
            tick=_make_tick(bid=2000.0, ask=2000.50),
            symbol="XAUUSD", side="buy", sl=1999.80,
        )
        sl_check = next(c for c in checks if c["name"] == "stops_level_sl")
        assert sl_check["passed"] is True

    def test_stops_level_tp_too_close(self):
        # stops_level=50, point=0.01 → min=0.50
        # buy at ask=2000.50, TP at 2000.70 → distance=0.20 < 0.50
        checks = self._call(
            symbol_info=_make_symbol_info(trade_stops_level=50, point=0.01),
            tick=_make_tick(bid=2000.0, ask=2000.50),
            symbol="XAUUSD", side="buy", tp=2000.70,
        )
        tp_check = next(c for c in checks if c["name"] == "stops_level_tp")
        assert tp_check["passed"] is False
        assert "TP too close" in tp_check["message"]

    def test_stops_level_tp_sufficient(self):
        # stops_level=50, point=0.01 → min=0.50
        # buy at ask=2000.50, TP at 2001.50 → distance=1.00 > 0.50
        checks = self._call(
            symbol_info=_make_symbol_info(trade_stops_level=50, point=0.01),
            tick=_make_tick(bid=2000.0, ask=2000.50),
            symbol="XAUUSD", side="buy", tp=2001.50,
        )
        tp_check = next(c for c in checks if c["name"] == "stops_level_tp")
        assert tp_check["passed"] is True

    def test_stops_level_zero_skips_check(self):
        # stops_level=0 → no SL/TP distance check
        checks = self._call(
            symbol_info=_make_symbol_info(trade_stops_level=0),
            symbol="XAUUSD", side="buy", sl=2000.49,
        )
        sl_checks = [c for c in checks if c["name"].startswith("stops_level")]
        assert len(sl_checks) == 0

    def test_stops_level_uses_request_price_when_provided(self):
        # request_price=2000.00, stops_level=30, point=0.01 → min=0.30
        # SL at 1999.80 → distance=0.20 < 0.30
        checks = self._call(
            symbol_info=_make_symbol_info(trade_stops_level=30, point=0.01),
            symbol="XAUUSD", side="buy",
            request_price=2000.00, sl=1999.80,
        )
        sl_check = next(c for c in checks if c["name"] == "stops_level_sl")
        assert sl_check["passed"] is False

    def test_stops_level_sell_side_uses_bid(self):
        # sell at bid=2000.0, stops_level=50, point=0.01 → min=0.50
        # SL at 2000.30 → distance=0.30 < 0.50
        checks = self._call(
            symbol_info=_make_symbol_info(trade_stops_level=50, point=0.01),
            tick=_make_tick(bid=2000.0, ask=2000.50),
            symbol="XAUUSD", side="sell", sl=2000.30,
        )
        sl_check = next(c for c in checks if c["name"] == "stops_level_sl")
        assert sl_check["passed"] is False

    # -- freeze_level tests ------------------------------------------------

    def test_freeze_level_reported_as_warning(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_freeze_level=20, point=0.01),
            symbol="XAUUSD", side="buy",
        )
        freeze_check = next(c for c in checks if c["name"] == "freeze_level")
        assert freeze_check["passed"] is True  # warning, not a block
        assert "20 points" in freeze_check["message"]

    def test_freeze_level_zero_no_check(self):
        checks = self._call(
            symbol_info=_make_symbol_info(trade_freeze_level=0),
            symbol="XAUUSD", side="buy",
        )
        freeze_checks = [c for c in checks if c["name"] == "freeze_level"]
        assert len(freeze_checks) == 0


# ---------------------------------------------------------------------------
# Integration: validate_trade_request raises on broker constraint failure
# ---------------------------------------------------------------------------

class TestValidateTradeRequestBrokerIntegration:

    def test_validate_raises_on_trade_mode_disabled(self):
        client = MT5TradingClient.__new__(MT5TradingClient)
        with patch("src.clients.mt5_trading.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = _make_symbol_info(trade_mode=0)
            mock_mt5.symbol_info_tick.return_value = _make_tick()
            mock_mt5.ORDER_TYPE_BUY = 0
            mock_mt5.ORDER_TYPE_SELL = 1
            client.connect = lambda: None

            with pytest.raises(MT5TradingClientError, match="Broker constraint violated"):
                client.validate_trade_request(
                    symbol="XAUUSD", volume=0.1, side="buy",
                )

    def test_validate_raises_on_sl_too_close(self):
        client = MT5TradingClient.__new__(MT5TradingClient)
        with patch("src.clients.mt5_trading.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = _make_symbol_info(
                trade_stops_level=50, point=0.01,
            )
            mock_mt5.symbol_info_tick.return_value = _make_tick(bid=2000.0, ask=2000.50)
            mock_mt5.ORDER_TYPE_BUY = 0
            mock_mt5.ORDER_TYPE_SELL = 1
            client.connect = lambda: None

            with pytest.raises(MT5TradingClientError, match="SL too close"):
                client.validate_trade_request(
                    symbol="XAUUSD", volume=0.1, side="buy",
                    sl=2000.30,  # distance=0.20 < min=0.50
                )

    def test_validate_passes_with_sufficient_distance(self):
        client = MT5TradingClient.__new__(MT5TradingClient)
        with patch("src.clients.mt5_trading.mt5") as mock_mt5:
            mock_mt5.symbol_info.return_value = _make_symbol_info(
                trade_stops_level=50, point=0.01,
            )
            mock_mt5.symbol_info_tick.return_value = _make_tick(bid=2000.0, ask=2000.50)
            mock_mt5.ORDER_TYPE_BUY = 0
            mock_mt5.ORDER_TYPE_SELL = 1
            client.connect = lambda: None

            result = client.validate_trade_request(
                symbol="XAUUSD", volume=0.1, side="buy",
                sl=1999.50,  # distance=1.00 > min=0.50
                tp=2001.50,  # distance=1.00 > min=0.50
            )
            assert result["pending"] is False
            assert any(c["name"] == "trade_mode" for c in result["broker_checks"])


# ---------------------------------------------------------------------------
# Integration: precheck_trade surfaces broker checks
# ---------------------------------------------------------------------------

class DummyTradingClientWithBrokerChecks:
    """Minimal stub that simulates check_broker_constraints."""

    def __init__(self, broker_checks: list[dict[str, Any]] | None = None) -> None:
        self._broker_checks = broker_checks or []

    def connect(self):
        return None

    def check_broker_constraints(self, **kwargs) -> list[dict[str, Any]]:
        return list(self._broker_checks)

    def validate_trade_request(self, **kwargs) -> dict:
        return {"order_type": 0, "request_price": 2000.50, "pending": False}

    def estimate_margin(self, **kwargs) -> float:
        return 500.0

    def side_and_kind_to_order_type(self, side, kind="market"):
        return 0


class DummyRiskService:
    def assess_trade(self, **kwargs):
        return {
            "enabled": True,
            "mode": "warn_only",
            "blocked": False,
            "verdict": "allow",
            "reason": None,
            "symbol": kwargs.get("symbol", "XAUUSD"),
            "active_windows": [],
            "upcoming_windows": [],
            "warnings": [],
            "checks": [],
            "intent": {},
        }

    def enforce_trade_allowed(self, **kwargs):
        return self.assess_trade(**kwargs)


class TestPrecheckBrokerConstraints:

    def test_precheck_blocked_by_trade_mode(self):
        client = DummyTradingClientWithBrokerChecks(broker_checks=[
            {"name": "trade_mode", "passed": False, "message": "Trading disabled (DISABLED)"},
        ])
        service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

        result = service.precheck_trade(symbol="XAUUSD", side="buy")
        assert result["blocked"] is True
        assert result["executable"] is False
        assert "DISABLED" in result["reason"]
        assert any(c["name"] == "trade_mode" and not c["passed"] for c in result["checks"])

    def test_precheck_blocked_by_stops_level(self):
        client = DummyTradingClientWithBrokerChecks(broker_checks=[
            {"name": "trade_mode", "passed": True, "message": "ok"},
            {"name": "stops_level_sl", "passed": False, "message": "SL too close: 0.20 < 0.50"},
        ])
        service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

        result = service.precheck_trade(
            symbol="XAUUSD", side="buy", sl=2000.30,
        )
        assert result["blocked"] is True
        assert "SL too close" in result["reason"]

    def test_precheck_passes_all_broker_checks(self):
        client = DummyTradingClientWithBrokerChecks(broker_checks=[
            {"name": "trade_mode", "passed": True, "message": "ok"},
            {"name": "stops_level_sl", "passed": True, "message": "ok"},
            {"name": "stops_level_tp", "passed": True, "message": "ok"},
        ])
        service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

        result = service.precheck_trade(
            symbol="XAUUSD", volume=0.1, side="buy",
            sl=1999.50, tp=2001.50,
        )
        assert result["blocked"] is False
        assert result["executable"] is True
        broker_checks = [c for c in result["checks"] if c["name"].startswith(("trade_mode", "stops_level"))]
        assert all(c["passed"] for c in broker_checks)

    def test_precheck_freeze_level_as_warning(self):
        client = DummyTradingClientWithBrokerChecks(broker_checks=[
            {"name": "trade_mode", "passed": True, "message": "ok"},
            {"name": "freeze_level", "passed": True, "message": "Freeze level = 20 points"},
        ])
        service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

        result = service.precheck_trade(symbol="XAUUSD", side="buy")
        assert result["blocked"] is False
        assert any("Freeze level" in w for w in result.get("warnings", []))

    def test_precheck_without_side_skips_broker_checks(self):
        """When side is not provided, broker checks are skipped (precheck still runs)."""
        client = DummyTradingClientWithBrokerChecks(broker_checks=[
            {"name": "trade_mode", "passed": False, "message": "should not appear"},
        ])
        service = TradingService(client=client, pre_trade_risk_service=DummyRiskService())

        result = service.precheck_trade(symbol="XAUUSD")
        # No side → broker checks skipped → not blocked by trade_mode
        assert result["blocked"] is False

    def test_precheck_broker_check_exception_becomes_warning(self):
        """If check_broker_constraints raises, it degrades to a warning, not a block."""
        class FailingClient(DummyTradingClientWithBrokerChecks):
            def check_broker_constraints(self, **kwargs):
                raise RuntimeError("MT5 connection lost")

        service = TradingService(
            client=FailingClient(),
            pre_trade_risk_service=DummyRiskService(),
        )

        result = service.precheck_trade(symbol="XAUUSD", side="buy")
        assert result["blocked"] is False
        assert any("unavailable" in w.lower() for w in result.get("warnings", []))
