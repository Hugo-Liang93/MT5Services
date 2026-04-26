"""WorkbenchReadModel 测试（P9 Phase 1）。

覆盖：
- 9 块 happy path（runtime present）
- account_alias 不匹配抛 KeyError
- include 过滤指定块
- exposure 按 (symbol, direction) 聚合
- marketContext 在 market_service 缺失时优雅降级
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from src.readmodels.workbench import (
    DEFAULT_FRESHNESS_HINTS,
    WORKBENCH_DEFAULT_BLOCKS,
    WorkbenchReadModel,
    compute_source_kind,
)


@dataclass
class _Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float
    time: Any


class _StubMarketService:
    def __init__(
        self, quote: Optional[_Quote] = None, spread_points: float = 12.0
    ) -> None:
        self._quote = quote
        self._spread_points = spread_points

    def get_quote(self, symbol: Optional[str] = None) -> Optional[_Quote]:
        return self._quote

    def get_current_spread(self, symbol: Optional[str] = None) -> float:
        return self._spread_points


class _StubRuntimeReadModel:
    """只提供 WorkbenchReadModel 真正调用的方法集，最小侵入。"""

    def __init__(
        self,
        *,
        positions_payload: Optional[dict] = None,
        risk_payload: Optional[dict] = None,
        executor_payload: Optional[dict] = None,
        tradability_payload: Optional[dict] = None,
        active_pending_payload: Optional[dict] = None,
        lifecycle_pending_payload: Optional[dict] = None,
        execution_contexts_payload: Optional[dict] = None,
        events_payload: Optional[dict] = None,
        pending_entries_payload: Optional[dict] = None,
    ) -> None:
        self._positions = positions_payload or {
            "count": 0,
            "items": [],
            "status_counts": {},
        }
        self._risk = risk_payload or {}
        self._executor = executor_payload or {}
        self._tradability = tradability_payload or {
            "verdict": "tradable",
            "reason_code": None,
            "tier": "T0",
        }
        self._active_pending = active_pending_payload or {
            "count": 0,
            "items": [],
            "status_counts": {},
        }
        self._lifecycle_pending = lifecycle_pending_payload or {"count": 0, "items": []}
        self._execution_contexts = execution_contexts_payload or {"items": []}
        self._events = events_payload or {"count": 0, "items": []}
        self._pending_entries = pending_entries_payload or {"entries": []}

    def position_runtime_state_payload(self, *, statuses=None, limit=20):
        items = list(self._positions.get("items") or [])
        if statuses:
            items = [i for i in items if i.get("status") in set(statuses)]
        return {
            "count": len(items),
            "status_counts": self._positions.get("status_counts") or {},
            "items": items,
        }

    def account_risk_state_summary(self):
        return dict(self._risk) if self._risk else {}

    def trade_executor_summary(self):
        return dict(self._executor)

    def tradability_state_summary(self):
        return dict(self._tradability)

    def active_pending_order_payload(self, *, limit=100):
        return dict(self._active_pending)

    def pending_order_lifecycle_payload(self, *, limit=100):
        return dict(self._lifecycle_pending)

    def pending_execution_context_payload(self):
        return dict(self._execution_contexts)

    def recent_trade_pipeline_events_payload(self, *, limit=50):
        items = list(self._events.get("items") or [])[:limit]
        return {"count": len(items), "items": items}

    def pending_entries_summary(self):
        return dict(self._pending_entries)


def _identity(alias: str = "live-main") -> SimpleNamespace:
    return SimpleNamespace(account_alias=alias)


def _full_native_quote() -> _Quote:
    """构造能让 marketContext 标 native 的完整 quote。"""
    return _Quote(
        symbol="XAUUSD",
        bid=2331.10,
        ask=2331.20,
        last=2331.15,
        volume=0.0,
        time=SimpleNamespace(isoformat=lambda: "2026-04-20T10:00:00+00:00"),
    )


def test_workbench_build_happy_path_returns_all_blocks() -> None:
    runtime = _StubRuntimeReadModel(
        # account_risk 非空 → risk 块 native（risk_payload 为空 dict 时也算 native，
        # 因为 store 仍然在）
        risk_payload={"updated_at": "2026-04-20T10:00:00+00:00"},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    payload = workbench.build(account_alias="live-main", symbol="XAUUSD")

    assert payload["account_alias"] == "live-main"
    assert payload["observed_at"]
    # P9 Phase 2.3: 全块 native → source.kind=live
    assert payload["source"]["kind"] == "live"
    assert payload["source"]["fallback_applied"] is False
    assert payload["source"]["fallback_reason"] is None
    assert set(payload["freshness_hints"].keys()) == set(DEFAULT_FRESHNESS_HINTS.keys())
    for block in WORKBENCH_DEFAULT_BLOCKS:
        assert block in payload, f"missing block: {block}"


def test_workbench_delegated_main_role_not_classified_as_fallback() -> None:
    """P2 回归：multi_account main role 不挂 trade_executor 是合法 delegated 拓扑，
    readmodel 已把 tradability.verdict 标为 'not_applicable' +
    executor.execution_scope='remote_executor' / state='delegated'。

    旧 _build_execution_block 仅看 runtime_present=False → 一律标 fallback →
    顶层 source.kind='hybrid'，把正常拓扑误报成降级状态。
    """
    runtime = _StubRuntimeReadModel(
        tradability_payload={
            "runtime_present": False,
            "admission_enabled": False,
            "verdict": "not_applicable",
            "reason_code": "not_executor_role",
            "reason": "本实例为 main role，不直接执行交易；"
            "请查询 executor 实例的 tradability",
            "circuit_open": False,
            "tradable": False,
        },
        executor_payload={
            "status": "disabled",
            "state": "delegated",
            "execution_scope": "remote_executor",
            "configured": False,
            "armed": False,
            "running": False,
            "enabled": False,
            "circuit_open": False,
            "consecutive_failures": 0,
            "execution_count": 0,
        },
        risk_payload={"updated_at": "2026-04-26T10:00:00+00:00"},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity("live-main"),
    )

    payload = workbench.build(
        account_alias="live-main", symbol="XAUUSD", include=["execution"]
    )

    # delegated 拓扑应被显式识别，而非 fallback
    assert payload["execution"]["source_kind"] != "fallback", (
        f"delegated topology 不应被标 fallback；execution block: "
        f"{payload['execution']!r}"
    )
    # 顶层 source 不应是 hybrid（因为 delegated 不是降级状态）
    assert payload["source"]["kind"] != "hybrid", (
        f"delegated topology 不应让顶层 source 变 hybrid；source: "
        f"{payload['source']!r}"
    )


def test_workbench_build_rejects_unknown_account_alias() -> None:
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(),
        runtime_identity=_identity("live-main"),
    )

    with pytest.raises(KeyError, match="not configured for this instance"):
        workbench.build(account_alias="other-account")


def test_workbench_build_allows_missing_runtime_identity() -> None:
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(),
        runtime_identity=None,
    )

    payload = workbench.build(account_alias="any-alias")

    assert payload["account_alias"] == "any-alias"


def test_workbench_build_include_filters_blocks() -> None:
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(),
        runtime_identity=_identity(),
    )

    payload = workbench.build(
        account_alias="live-main",
        include=["execution", "risk"],
    )

    assert "execution" in payload
    assert "risk" in payload
    assert "exposure" not in payload
    assert "marketContext" not in payload


def test_workbench_exposure_aggregates_by_symbol_and_direction() -> None:
    runtime = _StubRuntimeReadModel(
        positions_payload={
            "count": 3,
            "status_counts": {"open": 3},
            "items": [
                {
                    "symbol": "XAUUSD",
                    "direction": "buy",
                    "volume": 0.4,
                    "floating_pnl": 12.5,
                    "margin_used": 180.0,
                    "status": "open",
                },
                {
                    "symbol": "XAUUSD",
                    "direction": "buy",
                    "volume": 0.2,
                    "floating_pnl": -3.0,
                    "margin_used": 90.0,
                    "status": "open",
                },
                {
                    "symbol": "EURUSD",
                    "direction": "sell",
                    "volume": 1.0,
                    "floating_pnl": 5.0,
                    "margin_used": 120.0,
                    "status": "open",
                },
            ],
        }
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime, runtime_identity=_identity()
    )

    payload = workbench.build(account_alias="live-main", include=["exposure"])

    exposure = payload["exposure"]
    assert exposure["account_alias"] == "live-main"
    xau_long = next(
        s
        for s in exposure["symbols"]
        if s["symbol"] == "XAUUSD" and s["direction"] == "long"
    )
    assert xau_long["gross_volume"] == 0.6
    assert xau_long["floating_pnl"] == 9.5
    assert xau_long["margin_used"] == 270.0
    assert xau_long["position_count"] == 2

    eur_short = next(
        s
        for s in exposure["symbols"]
        if s["symbol"] == "EURUSD" and s["direction"] == "short"
    )
    assert eur_short["position_count"] == 1
    assert exposure["hotspots"][0]["symbol"] == "EURUSD"


def test_workbench_market_context_returns_quote_when_available() -> None:
    quote = _Quote(
        symbol="XAUUSD",
        bid=2331.10,
        ask=2331.20,
        last=2331.15,
        volume=0.0,
        time=SimpleNamespace(isoformat=lambda: "2026-04-20T10:00:00+00:00"),
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(),
        market_service=_StubMarketService(quote=quote, spread_points=10.0),
        runtime_identity=_identity(),
    )

    payload = workbench.build(
        account_alias="live-main",
        symbol="XAUUSD",
        include=["marketContext"],
    )

    market_ctx = payload["marketContext"]
    assert market_ctx["available"] is True
    assert market_ctx["bid"] == 2331.10
    assert market_ctx["ask"] == 2331.20
    assert market_ctx["spread_points"] == 10.0
    assert market_ctx["quote_updated_at"] == "2026-04-20T10:00:00+00:00"


def test_workbench_market_context_marks_unavailable_when_no_quote() -> None:
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(),
        market_service=_StubMarketService(quote=None),
        runtime_identity=_identity(),
    )

    payload = workbench.build(
        account_alias="live-main",
        symbol="XAUUSD",
        include=["marketContext"],
    )

    assert payload["marketContext"]["available"] is False
    assert payload["marketContext"]["reason"] == "no_quote"


def test_workbench_market_context_marks_unavailable_when_service_missing() -> None:
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(),
        market_service=None,
        runtime_identity=_identity(),
    )

    payload = workbench.build(
        account_alias="live-main",
        symbol="XAUUSD",
        include=["marketContext"],
    )

    assert payload["marketContext"]["available"] is False
    assert payload["marketContext"]["reason"] == "market_service_unavailable"


def test_workbench_related_objects_extracts_signals_and_commands() -> None:
    runtime = _StubRuntimeReadModel(
        pending_entries_payload={
            "entries": [
                {
                    "signal_id": "sig_1",
                    "symbol": "XAUUSD",
                    "timeframe": "H1",
                    "strategy": "trend",
                    "direction": "buy",
                },
                {"signal_id": None},  # 应被过滤
            ]
        },
        events_payload={
            "count": 3,
            "items": [
                {
                    "id": 1,
                    "command_id": "cmd_a",
                    "event_type": "command_submitted",
                    "recorded_at": "2026-04-20T10:00:00+00:00",
                },
                {
                    "id": 2,
                    "event_type": "intent_published",  # 应被过滤
                    "recorded_at": "2026-04-20T10:01:00+00:00",
                },
                {
                    "id": 3,
                    "command_id": "cmd_b",
                    "event_type": "command_completed",
                    "recorded_at": "2026-04-20T10:02:00+00:00",
                },
            ],
        },
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime, runtime_identity=_identity()
    )

    payload = workbench.build(account_alias="live-main", include=["relatedObjects"])

    related = payload["relatedObjects"]
    assert len(related["active_signals"]) == 1
    assert related["active_signals"][0]["signal_id"] == "sig_1"
    assert len(related["recent_commands"]) == 2
    assert {c["command_id"] for c in related["recent_commands"]} == {"cmd_a", "cmd_b"}


def test_workbench_stream_block_returns_sse_metadata() -> None:
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(), runtime_identity=_identity()
    )

    payload = workbench.build(account_alias="live-main", include=["stream"])

    stream = payload["stream"]
    assert stream["sse_url"] == "/v1/trade/state/stream?account=live-main"
    assert stream["supports_stream"] is True
    assert stream["recommended_poll_interval_seconds"] == 5


def test_workbench_positions_block_extracts_latest_updated_at() -> None:
    runtime = _StubRuntimeReadModel(
        positions_payload={
            "count": 2,
            "status_counts": {"open": 2},
            "items": [
                {"symbol": "XAUUSD", "updated_at": "2026-04-20T09:00:00+00:00"},
                {"symbol": "XAUUSD", "updated_at": "2026-04-20T10:30:00+00:00"},
            ],
        }
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime, runtime_identity=_identity()
    )

    payload = workbench.build(account_alias="live-main", include=["positions"])

    assert payload["positions"]["positions_updated_at"] == "2026-04-20T10:30:00+00:00"
    assert payload["positions"]["count"] == 2


# ── P9 Phase 2.3: source 聚合 ────────────────────────────────────


def test_compute_source_kind_returns_live_when_all_blocks_native() -> None:
    result = compute_source_kind(
        {
            "execution": {"source_kind": "native"},
            "risk": {"source_kind": "native"},
            "marketContext": {"source_kind": "native", "available": True},
        }
    )
    assert result == {
        "kind": "live",
        "fallback_applied": False,
        "fallback_reason": None,
    }


def test_compute_source_kind_returns_hybrid_when_any_block_fallback() -> None:
    result = compute_source_kind(
        {
            "execution": {"source_kind": "native"},
            "marketContext": {
                "source_kind": "fallback",
                "available": False,
                "reason": "no_quote",
            },
        }
    )
    assert result["kind"] == "hybrid"
    assert result["fallback_applied"] is True
    assert "marketContext" in result["fallback_reason"]


def test_compute_source_kind_skips_stream_block() -> None:
    """stream 块是静态 SSE 元数据，不参与 fallback 判定。"""
    result = compute_source_kind(
        {
            "execution": {"source_kind": "native"},
            "stream": {"source_kind": "static"},
        }
    )
    assert result["kind"] == "live"


def test_compute_source_kind_treats_available_false_as_fallback() -> None:
    """即使没显式 source_kind，available=False 也算降级。"""
    result = compute_source_kind(
        {
            "execution": {"source_kind": "native"},
            "marketContext": {"available": False, "reason": "no_quote"},
        }
    )
    assert result["kind"] == "hybrid"
    assert "marketContext" in result["fallback_reason"]
    assert "no_quote" in result["fallback_reason"]


def test_compute_source_kind_dedups_and_sorts_fallback_reasons() -> None:
    result = compute_source_kind(
        {
            "marketContext": {"source_kind": "fallback"},
            "risk": {"source_kind": "fallback"},
            "execution": {"source_kind": "native"},
        }
    )
    # 字母序排序便于前端稳定显示
    assert result["fallback_reason"] == "marketContext,risk"


def test_compute_source_kind_ignores_non_mapping_blocks() -> None:
    """防御性测试：非 dict 块不导致崩溃。"""
    result = compute_source_kind(
        {
            "execution": {"source_kind": "native"},
            "weird": "not a dict",  # type: ignore[dict-item]
        }
    )
    assert result["kind"] == "live"


def test_workbench_source_is_hybrid_when_market_service_missing() -> None:
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(
            risk_payload={"updated_at": "2026-04-20T10:00:00+00:00"},
        ),
        market_service=None,
        runtime_identity=_identity(),
    )

    payload = workbench.build(account_alias="live-main", symbol="XAUUSD")

    assert payload["source"]["kind"] == "hybrid"
    assert payload["source"]["fallback_applied"] is True
    assert "marketContext" in payload["source"]["fallback_reason"]


def test_workbench_source_is_live_when_marketcontext_excluded_even_without_market() -> (
    None
):
    """include 不含 marketContext 时，缺市场服务不影响 source.kind。"""
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(
            risk_payload={"updated_at": "2026-04-20T10:00:00+00:00"},
        ),
        market_service=None,
        runtime_identity=_identity(),
    )

    payload = workbench.build(
        account_alias="live-main",
        include=["execution", "risk", "positions"],
    )

    assert payload["source"]["kind"] == "live"


def test_workbench_execution_block_marks_fallback_when_runtime_not_present() -> None:
    """tradability.runtime_present=False → execution 块 fallback → 顶层 hybrid。"""
    runtime = _StubRuntimeReadModel(
        tradability_payload={
            "verdict": "blocked",
            "reason_code": "runtime_not_ready",
            "tier": "T0",
            "runtime_present": False,
        },
        risk_payload={"updated_at": "..."},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    payload = workbench.build(account_alias="live-main", symbol="XAUUSD")

    assert payload["execution"]["source_kind"] == "fallback"
    assert payload["source"]["kind"] == "hybrid"
    assert "execution" in payload["source"]["fallback_reason"]


def test_workbench_blocks_carry_native_source_kind_in_default_path() -> None:
    """默认场景下各块（除 marketContext 异常时）都应标 native；stream 标 static。"""
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntimeReadModel(
            risk_payload={"updated_at": "2026-04-20T10:00:00+00:00"},
        ),
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    payload = workbench.build(account_alias="live-main", symbol="XAUUSD")

    assert payload["execution"]["source_kind"] == "native"
    assert payload["risk"]["source_kind"] == "native"
    assert payload["positions"]["source_kind"] == "native"
    assert payload["orders"]["source_kind"] == "native"
    assert payload["pending"]["source_kind"] == "native"
    assert payload["exposure"]["source_kind"] == "native"
    assert payload["events"]["source_kind"] == "native"
    assert payload["relatedObjects"]["source_kind"] == "native"
    assert payload["marketContext"]["source_kind"] == "native"
    assert payload["stream"]["source_kind"] == "static"


# ── P9 Phase 3.1: 共享快照（扇出收敛） ─────────────────────────────


class _CountingRuntimeReadModel(_StubRuntimeReadModel):
    """记录每个底层方法被调用的次数，用于验证扇出收敛。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.call_counts: dict[str, int] = {}

    def _bump(self, name: str) -> None:
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    def position_runtime_state_payload(self, *, statuses=None, limit=20):
        self._bump("position_runtime_state_payload")
        return super().position_runtime_state_payload(statuses=statuses, limit=limit)

    def active_pending_order_payload(self, *, limit=100):
        self._bump("active_pending_order_payload")
        return super().active_pending_order_payload(limit=limit)

    def recent_trade_pipeline_events_payload(self, *, limit=50):
        self._bump("recent_trade_pipeline_events_payload")
        return super().recent_trade_pipeline_events_payload(limit=limit)


def test_workbench_build_shares_position_payload_across_blocks() -> None:
    """positions / exposure 块共享一次 position_runtime_state_payload 调用。"""
    runtime = _CountingRuntimeReadModel(
        risk_payload={"updated_at": "2026-04-20T10:00:00+00:00"},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    workbench.build(account_alias="live-main", symbol="XAUUSD")

    # P9 Phase 3.1: 改造前 positions/exposure 块各调一次 = 2 次；现合并为 1 次
    assert runtime.call_counts.get("position_runtime_state_payload") == 1


def test_workbench_build_shares_pending_payload_across_orders_and_pending_blocks() -> (
    None
):
    """orders / pending 块共享一次 active_pending_order_payload 调用。"""
    runtime = _CountingRuntimeReadModel(
        risk_payload={"updated_at": "..."},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    workbench.build(account_alias="live-main", symbol="XAUUSD")

    # 改造前 orders + pending 块各调一次 = 2 次；现合并为 1 次
    assert runtime.call_counts.get("active_pending_order_payload") == 1


def test_workbench_build_shares_pipeline_events_across_events_and_related_objects() -> (
    None
):
    """events / relatedObjects 块共享一次 recent_trade_pipeline_events_payload。"""
    runtime = _CountingRuntimeReadModel(
        risk_payload={"updated_at": "..."},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    workbench.build(account_alias="live-main", symbol="XAUUSD")

    assert runtime.call_counts.get("recent_trade_pipeline_events_payload") == 1


def test_workbench_build_resets_cache_between_calls() -> None:
    """每次 build() reset 缓存，确保拿到 fresh 数据。"""
    runtime = _CountingRuntimeReadModel(
        risk_payload={"updated_at": "..."},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    workbench.build(account_alias="live-main", symbol="XAUUSD")
    workbench.build(account_alias="live-main", symbol="XAUUSD")

    # 两次 build 各自 fetch 一次（不跨 build 共享）
    assert runtime.call_counts.get("position_runtime_state_payload") == 2
    assert runtime.call_counts.get("active_pending_order_payload") == 2
    assert runtime.call_counts.get("recent_trade_pipeline_events_payload") == 2


def test_workbench_build_skips_unused_blocks_in_fanout() -> None:
    """include 不含 positions/exposure → 不触发 position_runtime_state_payload 调用。"""
    runtime = _CountingRuntimeReadModel(
        risk_payload={"updated_at": "..."},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime,
        market_service=_StubMarketService(quote=_full_native_quote()),
        runtime_identity=_identity(),
    )

    workbench.build(
        account_alias="live-main",
        symbol="XAUUSD",
        include=["execution", "risk"],
    )

    assert runtime.call_counts.get("position_runtime_state_payload", 0) == 0
    assert runtime.call_counts.get("active_pending_order_payload", 0) == 0
    assert runtime.call_counts.get("recent_trade_pipeline_events_payload", 0) == 0


def test_workbench_exposure_block_filters_open_status_only_in_python() -> None:
    """共享 positions 含混合 status，exposure block 在 Python 层过滤 status='open'。"""
    runtime = _CountingRuntimeReadModel(
        positions_payload={
            "count": 3,
            "status_counts": {"open": 2, "closed": 1},
            "items": [
                {
                    "symbol": "XAUUSD",
                    "direction": "buy",
                    "volume": 0.5,
                    "status": "open",
                },
                {
                    "symbol": "EURUSD",
                    "direction": "sell",
                    "volume": 1.0,
                    "status": "open",
                },
                {
                    # 已关仓，不应进入 exposure 聚合
                    "symbol": "GBPUSD",
                    "direction": "buy",
                    "volume": 0.2,
                    "status": "closed",
                },
            ],
        },
        risk_payload={"updated_at": "..."},
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=runtime, runtime_identity=_identity()
    )

    payload = workbench.build(
        account_alias="live-main", include=["positions", "exposure"]
    )

    exposure_symbols = {s["symbol"] for s in payload["exposure"]["symbols"]}
    assert exposure_symbols == {"XAUUSD", "EURUSD"}  # GBPUSD closed 被过滤
    # 仅一次 store 调用支撑两个块
    assert runtime.call_counts["position_runtime_state_payload"] == 1
