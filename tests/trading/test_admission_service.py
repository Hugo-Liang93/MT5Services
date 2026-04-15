"""TradeAdmissionService 单元测试。

之前覆盖盲区（仅集成测试隐式覆盖）。本文件守住关键决策路径：
- precheck 通过/失败的 reason 形成
- tradability / circuit_open / quote_stale / intrabar_stale / event_blocked
  分别如何影响 decision 与 stage 优先级
- account_risk + last_risk_block=quote_stale 的去重边界
- trace_id 兜底链
- pipeline_event_bus emit 与 None 时静默
- deployment_contract / position_limits / account_alias 字段填充
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.trading.admission.service import (
    TradeAdmissionService,
    append_admission_report_event,
)


# ── 测试辅助 ────────────────────────────────────────────────────────────────


def _stub_command_service(
    *,
    verdict: str = "allow",
    checks: list[dict] | None = None,
    reason: str | None = None,
    event_blocked: bool = False,
    calendar_health_degraded: bool = False,
    calendar_health_mode: str = "warn_only",
    request_id: str | None = None,
    warnings: list[str] | None = None,
    active_account_alias: str = "live_main",
) -> Any:
    svc = MagicMock()
    svc.precheck_trade.return_value = {
        "verdict": verdict,
        "checks": checks or [],
        "reason": reason,
        "event_blocked": event_blocked,
        "calendar_health_degraded": calendar_health_degraded,
        "calendar_health_mode": calendar_health_mode,
        "calendar_health": {},
        "active_windows": [],
        "upcoming_windows": [],
        "warnings": warnings or [],
        "request_id": request_id,
    }
    svc.active_account_alias = active_account_alias
    return svc


def _stub_runtime_views(
    *,
    runtime_present: bool = True,
    admission_enabled: bool = True,
    circuit_open: bool = False,
    tradable: bool = True,
    quote_stale: bool = False,
    margin_guard: dict | None = None,
    last_risk_block: str = "",
    should_block_new_trades: bool = False,
    managed_positions_count: int = 0,
    runtime_identity_alias: str = "",
    runtime_identity_key: str = "",
) -> Any:
    views = MagicMock()
    views.tradability_state_summary.return_value = {
        "runtime_present": runtime_present,
        "admission_enabled": admission_enabled,
        "circuit_open": circuit_open,
        "tradable": tradable,
        "quote_health": {"stale": quote_stale} if quote_stale else {},
        "margin_guard": margin_guard or {},
    }
    views.account_risk_state_summary.return_value = {
        "should_block_new_trades": should_block_new_trades,
        "last_risk_block": last_risk_block,
        "margin_guard": margin_guard or {},
    }
    views.trade_control_summary.return_value = {"auto_entry_enabled": True}
    views.trading_state_summary.return_value = {
        "managed_positions": {"count": managed_positions_count}
    }
    views.runtime_identity = SimpleNamespace(
        account_alias=runtime_identity_alias,
        account_key=runtime_identity_key,
    )
    return views


def _make_service(
    *, command_svc=None, runtime_views=None, pipeline_event_bus=None
) -> TradeAdmissionService:
    return TradeAdmissionService(
        command_service=command_svc or _stub_command_service(),
        runtime_views=runtime_views or _stub_runtime_views(),
        pipeline_event_bus=pipeline_event_bus,
    )


# ── 基础决策路径 ────────────────────────────────────────────────────────────


def test_clean_payload_yields_allow_decision() -> None:
    """precheck 通过 + tradability 健康 → decision=allow, reasons 为空。"""
    svc = _make_service()
    result = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD", "strategy": "structured_trend_continuation"},
        requested_operation="market",
    )
    assert result["report"]["decision"] == "allow"
    assert result["report"]["reasons"] == []


def test_failed_check_produces_warn_reason() -> None:
    """precheck 有 failed_check → 形成 reason，decision=warn（如果 tradable 仍 True）。"""
    cmd = _stub_command_service(
        checks=[{"name": "max_volume_per_order", "passed": False, "message": "0.5 > 0.1"}]
    )
    svc = _make_service(command_svc=cmd)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    assert any(r["code"] == "max_volume_per_order" for r in report["reasons"])
    assert report["decision"] == "warn"


def test_runtime_absent_blocks_with_market_tradability_stage() -> None:
    """runtime_present=False → market_tradability stage reason → decision=block。"""
    views = _stub_runtime_views(runtime_present=False, tradable=False)
    svc = _make_service(runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    assert any(r["code"] == "runtime_absent" for r in report["reasons"])
    assert report["stage"] == "market_tradability"
    assert report["decision"] == "block"


def test_circuit_open_yields_account_risk_reason() -> None:
    """circuit_open=True → account_risk stage reason；
    若 tradable 仍 True 则 decision=warn。"""
    views = _stub_runtime_views(circuit_open=True)
    svc = _make_service(runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    assert any(r["code"] == "circuit_open" for r in report["reasons"])
    assert report["decision"] == "warn"


def test_quote_stale_marks_market_tradability() -> None:
    """quote_health.stale=True → market_tradability stage reason。"""
    views = _stub_runtime_views(quote_stale=True, tradable=False)
    svc = _make_service(runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    quote_reason = next((r for r in report["reasons"] if r["code"] == "quote_stale"), None)
    assert quote_reason is not None
    assert quote_reason["stage"] == "market_tradability"
    assert report["decision"] == "block"


def test_event_blocked_forces_block_decision() -> None:
    """precheck 返回 event_blocked=True 且 tradable=False → decision=block。"""
    cmd = _stub_command_service(event_blocked=True, reason="NFP imminent")
    views = _stub_runtime_views(tradable=False)
    svc = _make_service(command_svc=cmd, runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    assert report["economic_guard"]["event_blocked"] is True
    assert report["decision"] == "block"


def test_calendar_health_degraded_adds_reason() -> None:
    """calendar_health_degraded=True → 形成 calendar_health_degraded reason。"""
    cmd = _stub_command_service(calendar_health_degraded=True)
    svc = _make_service(command_svc=cmd)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    assert any(r["code"] == "calendar_health_degraded" for r in report["reasons"])


# ── account_risk 与 quote_stale 边界 ─────────────────────────────────────


def test_account_risk_block_adds_reason_when_not_quote_stale() -> None:
    """account_risk 阻断且 last_risk_block != quote_stale → risk_block_new_trades reason。"""
    views = _stub_runtime_views(
        should_block_new_trades=True, last_risk_block="daily_loss_limit_exceeded"
    )
    svc = _make_service(runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    risk_reason = next(
        (r for r in report["reasons"] if r["code"] == "risk_block_new_trades"), None
    )
    assert risk_reason is not None
    assert risk_reason["stage"] == "account_risk"


def test_account_risk_quote_stale_dedup_skips_redundant_reason() -> None:
    """last_risk_block == quote_stale 时不追加 risk_block_new_trades（去重），
    避免和 quote_stale reason 重复显示同一原因。"""
    views = _stub_runtime_views(
        should_block_new_trades=True,
        last_risk_block="quote_stale",
        quote_stale=True,
    )
    svc = _make_service(runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    # quote_stale reason 出现，但 risk_block_new_trades 不出现
    codes = [r["code"] for r in report["reasons"]]
    assert "quote_stale" in codes
    assert "risk_block_new_trades" not in codes


# ── trace_id 兜底链 ───────────────────────────────────────────────────────


def test_resolved_trace_id_uses_provided_trace_id_first() -> None:
    """trace_id 显式传入 → 直接用。"""
    svc = _make_service()
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"},
        requested_operation="market",
        trace_id="trace_abc",
        signal_id="sig_xyz",
    )["report"]
    assert report["trace_id"] == "trace_abc"


def test_resolved_trace_id_falls_back_through_chain() -> None:
    """trace_id 缺失 → fallback 到 signal_id → intent_id → action_id → request_id → 自生成。"""
    svc = _make_service()
    # 仅给 signal_id
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"},
        requested_operation="market",
        signal_id="sig_a",
    )["report"]
    assert report["trace_id"] == "sig_a"

    # 都没有 → 自生成 admission_<op>_<ts>
    cmd_no_request = _stub_command_service(request_id=None)
    svc2 = _make_service(command_svc=cmd_no_request)
    report2 = svc2.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="signal"
    )["report"]
    assert report2["trace_id"].startswith("admission_signal_")


# ── pipeline_event_bus ────────────────────────────────────────────────────


def test_emits_admission_report_when_event_bus_present() -> None:
    """有 event bus + 有 trace_id → emit admission_report_appended。"""
    bus = MagicMock()
    svc = _make_service(pipeline_event_bus=bus)
    svc.evaluate_trade_payload(
        {"symbol": "XAUUSD", "timeframe": "M30"},
        requested_operation="market",
        trace_id="trace_1",
    )
    assert bus.emit.called
    event_arg = bus.emit.call_args[0][0]
    assert event_arg.type == "admission_report_appended"
    assert event_arg.trace_id == "trace_1"


def test_does_not_emit_when_event_bus_is_none() -> None:
    """无 event bus → 不抛、不 emit、正常返回 report。"""
    svc = _make_service(pipeline_event_bus=None)
    result = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market", trace_id="trace_2"
    )
    assert result["report"]["decision"] == "allow"


def test_append_admission_report_event_skips_when_trace_id_blank() -> None:
    """trace_id 为空字符串 → 不 emit（保护 detached trace 形成）。"""
    bus = MagicMock()
    append_admission_report_event(
        pipeline_event_bus=bus,
        trace_id="",
        symbol="XAUUSD",
        timeframe="M30",
        scope="confirmed",
        report={"decision": "allow"},
    )
    bus.emit.assert_not_called()


# ── 字段填充 ──────────────────────────────────────────────────────────────


def test_deployment_contract_fields_extracted_from_payload() -> None:
    """deployment_contract 从 payload 提取关键字段。"""
    svc = _make_service()
    report = svc.evaluate_trade_payload(
        {
            "symbol": "XAUUSD",
            "strategy": "structured_trend_h4",
            "timeframe": "H1",
            "locked_sessions": ["london"],
            "locked_timeframes": ["H1"],
            "require_pending_entry": True,
            "max_live_positions": 1,
        },
        requested_operation="market",
    )["report"]
    contract = report["deployment_contract"]
    assert contract["strategy"] == "structured_trend_h4"
    assert contract["timeframe"] == "H1"
    assert contract["locked_sessions"] == ["london"]
    assert contract["require_pending_entry"] is True
    assert contract["max_live_positions"] == 1
    assert contract["applicable"] is True


def test_position_limits_filters_position_related_failures() -> None:
    """position_limits.checks 仅含名字含 'position' 或 'limit' 的 failed_checks。"""
    cmd = _stub_command_service(
        checks=[
            {"name": "max_positions_per_symbol", "passed": False, "message": "limit hit"},
            {"name": "spread_check", "passed": False, "message": "wide"},
            {"name": "trade_frequency_limit", "passed": False, "message": "too fast"},
        ]
    )
    views = _stub_runtime_views(managed_positions_count=2)
    svc = _make_service(command_svc=cmd, runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    pl_checks = report["position_limits"]["checks"]
    pl_names = {c.get("name") for c in pl_checks}
    assert "max_positions_per_symbol" in pl_names
    assert "trade_frequency_limit" in pl_names
    assert "spread_check" not in pl_names  # 不含 position/limit 字眼
    assert report["position_limits"]["managed_positions"] == 2


def test_account_alias_falls_back_to_runtime_identity() -> None:
    """payload 无 account_alias / assessment 无 → 用 runtime_identity.account_alias。"""
    cmd = _stub_command_service(active_account_alias="cmd_default")
    views = _stub_runtime_views(
        runtime_identity_alias="live_main",
        runtime_identity_key="live:trademaxglobal-live:50256386",
    )
    svc = _make_service(command_svc=cmd, runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD"}, requested_operation="market"
    )["report"]
    assert report["account_alias"] == "live_main"
    assert report["account_key"] == "live:trademaxglobal-live:50256386"


def test_account_alias_payload_overrides_runtime_identity() -> None:
    """payload 显式 account_alias → 优先于 runtime_identity。"""
    views = _stub_runtime_views(runtime_identity_alias="live_main")
    svc = _make_service(runtime_views=views)
    report = svc.evaluate_trade_payload(
        {"symbol": "XAUUSD", "account_alias": "live_exec_a"},
        requested_operation="market",
    )["report"]
    assert report["account_alias"] == "live_exec_a"
