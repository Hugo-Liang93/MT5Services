from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.readmodels.trades_workbench import TradesWorkbenchReadModel


class _FakeSignalRepo:
    def __init__(self) -> None:
        self.page_kwargs: dict[str, Any] = {}
        self.summary_kwargs: dict[str, Any] = {}
        self.page_payload: dict[str, Any] = {
            "items": [
                {
                    "recorded_at": "2026-04-20T10:00:00+00:00",
                    "signal_id": "sig_1",
                    "symbol": "XAUUSD",
                    "timeframe": "H1",
                    "strategy": "breakout_follow",
                    "direction": "buy",
                    "confidence": 0.72,
                    "account_alias": "live_main",
                    "account_key": "live:broker:1001",
                    "intent_id": "intent_1",
                    "fill_price": 2400.5,
                    "close_price": 2410.0,
                    "price_change": 9.5,
                    "won": True,
                    "regime": "trend",
                    "metadata": {},
                }
            ],
            "total": 1,
            "page": 1,
            "page_size": 50,
        }
        self.summary_payload: dict[str, Any] = {
            "total_count": 1,
            "win_count": 1,
            "loss_count": 0,
            "win_rate": 1.0,
            "gross_price_change": 9.5,
            "avg_price_change": 9.5,
            "last_recorded_at": "2026-04-20T10:00:00+00:00",
        }

    def query_trade_outcomes_page(self, **kwargs):
        self.page_kwargs = kwargs
        return self.page_payload

    def fetch_trade_outcomes_summary(self, **kwargs):
        self.summary_kwargs = kwargs
        return self.summary_payload


class _FakeTraceReadModel:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload
        self.called_with: str | None = None

    def trace_by_signal_id(self, signal_id: str) -> dict[str, Any] | None:
        self.called_with = signal_id
        return self._payload


def _build_workbench(signal_repo, trace_model) -> TradesWorkbenchReadModel:
    return TradesWorkbenchReadModel(
        signal_repo=signal_repo,
        trace_read_model=trace_model,
        account_alias_getter=lambda: "live_main",
    )


def test_build_workbench_returns_canonical_contract() -> None:
    signal_repo = _FakeSignalRepo()
    workbench = _build_workbench(signal_repo, _FakeTraceReadModel(None))

    payload = workbench.build_workbench(
        symbol="XAUUSD",
        strategy="breakout_follow",
        page=2,
        page_size=25,
        sort="recorded_at_asc",
    )

    assert payload["account_alias"] == "live_main"
    assert signal_repo.page_kwargs["symbol"] == "XAUUSD"
    assert signal_repo.page_kwargs["strategy"] == "breakout_follow"
    assert signal_repo.page_kwargs["page"] == 2
    assert signal_repo.page_kwargs["sort"] == "recorded_at_asc"
    assert payload["pagination"]["total"] == 1
    assert payload["summary"]["win_rate"] == 1.0
    record = payload["records"][0]
    assert record["trade_id"] == "sig_1"
    assert record["signal_id"] == "sig_1"
    assert record["pnl_percent"] == pytest.approx(9.5 / 2400.5 * 100.0, rel=1e-6)
    freshness = payload["freshness"]
    assert freshness["source_kind"] == "native"
    assert freshness["fallback_applied"] is False
    assert freshness["freshness_state"] in {"fresh", "warn", "stale"}


def test_build_trade_detail_returns_six_dimensions() -> None:
    trace_payload = {
        "found": True,
        "trace_id": "trace_42",
        "identifiers": {"signal_id": "sig_1"},
        "summary": {"stages": {}},
        "timeline": [{"id": "t1", "stage": "signal.confirmed", "at": "2026-04-20T09:00:00+00:00"}],
        "facts": {
            "signal_confirmed": {
                "signal_id": "sig_1",
                "direction": "buy",
                "confidence": 0.72,
                "strategy": "breakout_follow",
                "generated_at": "2026-04-20T08:55:00+00:00",
                "reason": "breakout above 2400",
                "metadata": {
                    "entry_spec": {
                        "entry_type": "market",
                        "entry_price": 2400.0,
                        "stop_loss": 2395.0,
                        "take_profit": 2410.0,
                        "volume": 0.1,
                    },
                    "indicators": {"atr14": 5.1},
                },
            },
            "signal_preview": None,
            "signal_preview_events": [],
            "signal_confirmed_events": [],
            "admission_reports": [
                {
                    "payload": {
                        "decision": "allow",
                        "stage": "pre_trade",
                    }
                }
            ],
            "pipeline_trace_events": [
                {
                    "event_type": "pre_trade.admission_allowed",
                    "recorded_at": "2026-04-20T09:00:00+00:00",
                    "payload": {},
                },
                {
                    "event_type": "risk.blocked",
                    "recorded_at": "2026-04-20T09:00:01+00:00",
                    "payload": {"reason": "spread_too_wide"},
                },
            ],
            "auto_executions": [
                {
                    "intent_id": "intent_1",
                    "entry_price": 2400.5,
                    "stop_loss": 2395.5,
                    "take_profit": 2410.5,
                    "volume": 0.1,
                    "success": True,
                    "executed_at": "2026-04-20T09:00:05+00:00",
                    "account_alias": "live_main",
                    "account_key": "live:broker:1001",
                }
            ],
            "trade_command_audits": [
                {
                    "operation_id": "op_1",
                    "command_type": "execute_trade",
                    "status": "success",
                    "recorded_at": datetime(2026, 4, 20, 9, 0, 5, tzinfo=timezone.utc),
                    "symbol": "XAUUSD",
                    "side": "buy",
                    "request_payload": {
                        "action_id": "act_1",
                        "idempotency_key": "idem_1",
                        "actor": "executor",
                        "reason": "auto",
                    },
                    "response_payload": {},
                }
            ],
            "pending_orders": [],
            "positions": [
                {
                    "ticket": 12345,
                    "volume": 0.1,
                    "open_price": 2400.5,
                    "current_price": 2410.0,
                    "sl": 2395.5,
                    "tp": 2410.5,
                    "status": "closed",
                    "profit": 9.5,
                }
            ],
            "signal_outcomes": [],
            "trade_outcomes": [
                {
                    "fill_price": 2400.5,
                    "close_price": 2410.0,
                    "price_change": 9.5,
                    "won": True,
                    "regime": "trend",
                    "recorded_at": "2026-04-20T10:00:00+00:00",
                }
            ],
        },
    }
    trace_model = _FakeTraceReadModel(trace_payload)
    workbench = _build_workbench(_FakeSignalRepo(), trace_model)

    detail = workbench.build_trade_detail(trade_id="sig_1")

    assert detail is not None
    assert detail["trade_id"] == "sig_1"
    assert detail["trace_id"] == "trace_42"
    assert trace_model.called_with == "sig_1"
    # plan_vs_live
    plan_vs_live = detail["plan_vs_live"]
    assert plan_vs_live["plan"]["entry_price"] == 2400.0
    assert plan_vs_live["live"]["entry_price"] == 2400.5
    assert plan_vs_live["outcome"]["won"] is True
    assert plan_vs_live["position_state"]["ticket"] == 12345
    # lifecycle
    assert detail["lifecycle"]["positions"][0]["ticket"] == 12345
    # risk_review
    risk = detail["risk_review"]
    assert risk["last_decision"] == "allow"
    assert any(
        "block" in str(event.get("event_type") or "").lower()
        for event in risk["risk_events"]
    )
    # receipts
    receipts = detail["receipts"]
    assert receipts["count"] == 1
    entry = receipts["entries"][0]
    assert entry["audit_id"] == "op_1"
    assert entry["action_id"] == "act_1"
    assert entry["idempotency_key"] == "idem_1"
    # evidence
    evidence = detail["evidence"]
    assert evidence["strategy"] == "breakout_follow"
    assert evidence["indicator_snapshot"] == {"atr14": 5.1}
    # linked_account_state — P2.2 从 auto_executions 提取真实账户上下文
    linked = detail["linked_account_state"]
    assert linked["count"] >= 1
    executing = linked["executing_accounts"]
    assert any(acc["account_alias"] == "live_main" for acc in executing)
    assert "note" in linked  # 契约字段显式声明数据源范围
    # freshness
    assert detail["freshness"]["source_kind"] == "native"


def test_build_trade_detail_is_a_pure_trace_adapter() -> None:
    """P2.1 契约守护：TradesWorkbench.build_trade_detail 仅消费
    TradingFlowTraceReadModel.trace_by_signal_id() 的 facts，**不做任何独立 DB 调用**。

    若本测试失败，说明有人给 trade detail 加了新数据源 —— 请重新评估是否该合并
    回 TradingFlowTraceReadModel，或者显式声明成独立读模型。
    """
    trace_model = _FakeTraceReadModel(
        {
            "found": True,
            "trace_id": "trace_x",
            "identifiers": {},
            "summary": {},
            "timeline": [],
            "facts": {
                "signal_confirmed": None,
                "signal_preview": None,
                "auto_executions": [],
                "trade_command_audits": [],
                "pending_orders": [],
                "positions": [],
                "trade_outcomes": [],
                "signal_outcomes": [],
                "pipeline_trace_events": [],
                "admission_reports": [],
            },
        }
    )
    # 故意传一个"如被访问就报错"的 signal_repo，证明 detail 不走 signal_repo
    class _ExplodingRepo:
        def __getattr__(self, name: str):
            raise AssertionError(
                f"trade detail MUST NOT call signal_repo.{name} — it's a trace adapter"
            )

    workbench = TradesWorkbenchReadModel(
        signal_repo=_ExplodingRepo(),
        trace_read_model=trace_model,
        account_alias_getter=lambda: "live_main",
    )

    detail = workbench.build_trade_detail(trade_id="sig_x")

    assert detail is not None
    assert detail["trade_id"] == "sig_x"
    # 只要不 AssertionError 炸 ExplodingRepo，就证明没有走 signal_repo


def test_build_trade_detail_returns_none_for_unknown_trade() -> None:
    trace_model = _FakeTraceReadModel({"found": False})
    workbench = _build_workbench(_FakeSignalRepo(), trace_model)

    assert workbench.build_trade_detail(trade_id="missing") is None
    assert workbench.build_trade_detail(trade_id="   ") is None
