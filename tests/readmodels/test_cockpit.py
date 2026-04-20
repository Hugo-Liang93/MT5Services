from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.readmodels.cockpit import (
    COCKPIT_BLOCKS,
    TRIAGE_LAYER_CRITICAL,
    TRIAGE_LAYER_HEALTHY,
    TRIAGE_LAYER_WARN,
    CockpitReadModel,
)


def _now_iso(offset_seconds: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


class _FakeTradingStateRepo:
    def __init__(
        self,
        *,
        risk_states: list[dict[str, Any]],
        exposure_rows: list[dict[str, Any]],
    ) -> None:
        self._risk_states = risk_states
        self._exposure_rows = exposure_rows

    def fetch_latest_risk_state_per_account(self) -> list[dict[str, Any]]:
        return list(self._risk_states)

    def aggregate_open_positions_by_account_symbol(self) -> list[dict[str, Any]]:
        return list(self._exposure_rows)


class _FakeSignalModule:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.page_kwargs: dict[str, Any] | None = None

    def recent_signal_page(self, **kwargs: Any) -> dict[str, Any]:
        self.page_kwargs = kwargs
        return {"items": self._rows, "total": len(self._rows), "page": 1, "page_size": 10}


def _build_model(
    *,
    risk_states: list[dict[str, Any]] | None = None,
    exposure_rows: list[dict[str, Any]] | None = None,
    signals: list[dict[str, Any]] | None = None,
) -> CockpitReadModel:
    return CockpitReadModel(
        trading_state_repo=_FakeTradingStateRepo(
            risk_states=risk_states or [],
            exposure_rows=exposure_rows or [],
        ),
        signal_module=_FakeSignalModule(signals),
        runtime_read_model=None,
    )


def test_cockpit_returns_all_seven_blocks() -> None:
    model = _build_model(risk_states=[], exposure_rows=[])
    payload = model.build_overview()

    for block in COCKPIT_BLOCKS:
        assert block in payload
    assert payload["source"]["kind"] == "live"


def test_decision_verdict_blocked_when_any_account_blocks_new_trades() -> None:
    model = _build_model(
        risk_states=[
            {
                "account_alias": "live_main",
                "account_key": "live:broker:1001",
                "should_block_new_trades": True,
                "circuit_open": False,
                "close_only_mode": False,
                "should_emergency_close": False,
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "live_exec_a",
                "account_key": "live:broker:1002",
                "should_block_new_trades": False,
                "circuit_open": False,
                "close_only_mode": False,
                "should_emergency_close": False,
                "updated_at": _now_iso(),
            },
        ]
    )
    payload = model.build_overview(include=["decision"])

    decision = payload["decision"]
    assert decision["can_trade"] is False
    assert decision["verdict"] == "blocked"
    assert "block_new_trades" in decision["reason_codes"]
    assert "live_main" in decision["blocked_accounts"]


def test_triage_queue_classifies_and_sorts_by_severity() -> None:
    model = _build_model(
        risk_states=[
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "should_block_new_trades": False,
                "circuit_open": False,
                "close_only_mode": False,
                "should_emergency_close": False,
                "active_risk_flags": [],
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "should_emergency_close": True,
                "circuit_open": True,
                "should_block_new_trades": True,
                "close_only_mode": True,
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "demo_main",
                "account_key": "demo:1",
                "close_only_mode": True,
                "should_block_new_trades": False,
                "circuit_open": False,
                "should_emergency_close": False,
                "updated_at": _now_iso(),
            },
        ]
    )
    payload = model.build_overview(include=["triage_queue"])

    triage = payload["triage_queue"]
    entries = triage["entries"]
    assert entries[0]["account_alias"] == "live_main"
    assert entries[0]["priority_layer"] == TRIAGE_LAYER_CRITICAL
    assert entries[0]["reason_code"] == "emergency_close_required"
    # warn 层（demo close-only）排第二
    assert entries[1]["account_alias"] == "demo_main"
    assert entries[1]["priority_layer"] == TRIAGE_LAYER_WARN
    # 健康账户最后
    assert entries[-1]["priority_layer"] == TRIAGE_LAYER_HEALTHY


def test_exposure_map_aggregates_contributors_across_accounts() -> None:
    model = _build_model(
        exposure_rows=[
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.6,
                "position_count": 3,
            },
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.4,
                "position_count": 2,
            },
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "EURUSD",
                "direction": "sell",
                "gross_volume": 1.0,
                "position_count": 1,
            },
        ]
    )
    payload = model.build_overview(include=["exposure_map"])

    entries = payload["exposure_map"]["entries"]
    # XAUUSD/buy 应合并为 2 contributors
    xau_bucket = next(e for e in entries if e["symbol"] == "XAUUSD" and e["direction"] == "buy")
    assert xau_bucket["gross_exposure"] == pytest.approx(1.0)
    assert len(xau_bucket["contributors"]) == 2
    weights = {c["account_alias"]: c["weight"] for c in xau_bucket["contributors"]}
    assert weights["live_main"] == pytest.approx(0.6)
    assert weights["live_exec_a"] == pytest.approx(0.4)


class _FakeIntelReadModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_action_queue(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "observed_at": "2026-04-20T10:00:00+00:00",
            "entries": [
                {
                    "id": "sig_x",
                    "signal_id": "sig_x",
                    "account_candidates": [{"account_alias": "live_main"}],
                    "recommended_action": "execute_from_signal",
                }
            ],
            "pagination": {"page": 1, "page_size": 10, "total": 1},
            "freshness": {"data_updated_at": "2026-04-20T09:59:00+00:00"},
        }


def test_opportunity_queue_delegates_to_intel_when_injected() -> None:
    """P1.3: 注入 IntelReadModel 后，opportunity_queue 直接返回 Intel 丰富后的 entries。"""
    intel = _FakeIntelReadModel()
    model = CockpitReadModel(
        trading_state_repo=_FakeTradingStateRepo(risk_states=[], exposure_rows=[]),
        signal_module=_FakeSignalModule([]),  # 不应该被调用
        runtime_read_model=None,
        intel_read_model=intel,
    )

    payload = model.build_overview(include=["opportunity_queue"])

    queue = payload["opportunity_queue"]
    assert len(queue["entries"]) == 1
    assert queue["entries"][0]["recommended_action"] == "execute_from_signal"
    assert queue["entries"][0]["account_candidates"] == [
        {"account_alias": "live_main"}
    ]
    # 确认有委托调用
    assert len(intel.calls) == 1
    assert intel.calls[0]["page_size"] == model.DEFAULT_MAX_OPPORTUNITY


def test_opportunity_queue_uses_priority_desc_and_actionable_filter() -> None:
    signals = [
        {
            "signal_id": "sig_a",
            "trace_id": "trace_a",
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "strategy": "breakout_follow",
            "direction": "buy",
            "confidence": 0.72,
            "priority": 0.72,
            "rank_source": "native",
            "generated_at": _now_iso(),
        }
    ]
    signal_module = _FakeSignalModule(signals)
    model = CockpitReadModel(
        trading_state_repo=_FakeTradingStateRepo(risk_states=[], exposure_rows=[]),
        signal_module=signal_module,
        runtime_read_model=None,
    )

    payload = model.build_overview(include=["opportunity_queue"])

    assert signal_module.page_kwargs is not None
    assert signal_module.page_kwargs["actionability"] == "actionable"
    assert signal_module.page_kwargs["sort"] == "priority_desc"
    assert len(payload["opportunity_queue"]["entries"]) == 1


def test_safe_actions_prioritizes_emergency_over_block() -> None:
    model = _build_model(
        risk_states=[
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "should_emergency_close": True,
                "circuit_open": True,
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "should_emergency_close": False,
                "circuit_open": True,
                "updated_at": _now_iso(),
            },
        ]
    )
    payload = model.build_overview(include=["safe_actions"])

    actions = payload["safe_actions"]["entries"]
    live_main_actions = [a for a in actions if a["account_alias"] == "live_main"]
    # live_main 只记 emergency（更高优先级），不记 circuit
    assert len(live_main_actions) == 1
    assert live_main_actions[0]["action"] == "emergency_close_all"
    # live_exec_a 记 circuit review
    live_exec_actions = [a for a in actions if a["account_alias"] == "live_exec_a"]
    assert live_exec_actions[0]["action"] == "review_circuit_breaker"


def test_cockpit_include_csv_limits_blocks() -> None:
    model = _build_model(risk_states=[], exposure_rows=[])

    payload = model.build_overview(include=["decision", "market_guard"])

    assert "decision" in payload
    assert "market_guard" in payload
    assert "exposure_map" not in payload
    assert "opportunity_queue" not in payload
