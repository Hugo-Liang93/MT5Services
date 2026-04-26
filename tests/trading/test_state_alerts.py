from __future__ import annotations

from src.trading.state import TradingStateAlerts


class DummyStateStore:
    def list_pending_order_states(self, *, statuses=None, limit=100):
        rows = [
            {"order_ticket": 101, "status": "placed"},
            {"order_ticket": 102, "status": "orphan"},
            {"order_ticket": 103, "status": "missing"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]

    def list_position_runtime_states(self, *, statuses=None, limit=100):
        rows = [
            {"position_ticket": 201, "status": "open"},
        ]
        if statuses:
            rows = [row for row in rows if row["status"] in set(statuses)]
        return rows[:limit]


class DummyTradingModule:
    active_account_alias = "default"

    def get_orders(self):
        return [{"ticket": 501}]

    def get_positions(self):
        return [{"ticket": 601}, {"ticket": 602}]


def test_trading_state_alerts_reports_state_gaps_and_count_mismatches() -> None:
    alerts = TradingStateAlerts(
        state_store=DummyStateStore(),
        trading_module=DummyTradingModule(),
        account_alias_getter=lambda: "default",
    )

    summary = alerts.summary()

    assert summary["status"] == "critical"
    codes = {item["code"] for item in summary["alerts"]}
    assert "pending_missing" in codes
    assert "pending_orphan" in codes
    assert "pending_active_mismatch" in codes
    assert "unmanaged_live_positions" in codes
    assert summary["observed"]["active_pending_count"] == 2
    assert summary["observed"]["live_position_count"] == 2


# ── P1 回归：双数据源失败不能伪装成 healthy ─────────────────────────────────


class _BrokenStateStore:
    def list_pending_order_states(self, *, statuses=None, limit=100):
        raise RuntimeError("DB unavailable")

    def list_position_runtime_states(self, *, statuses=None, limit=100):
        raise RuntimeError("DB unavailable")


class _BrokenTradingModule:
    active_account_alias = "default"

    def get_orders(self):
        raise RuntimeError("MT5 disconnected")

    def get_positions(self):
        raise RuntimeError("MT5 disconnected")


def test_trading_state_alerts_does_not_report_healthy_when_data_sources_fail() -> None:
    """P1 回归：state_store + trading_module 同时抛异常时，旧 _safe_* 全部
    返回空列表 → summary() 算出空集合 → status='healthy' + alerts=[]。
    把 DB/MT5 双故障伪装成"系统无异常"，掩盖真实严重故障。
    """
    alerts = TradingStateAlerts(
        state_store=_BrokenStateStore(),
        trading_module=_BrokenTradingModule(),
        account_alias_getter=lambda: "default",
    )

    summary = alerts.summary()

    assert summary["status"] != "healthy", (
        f"双数据源失败不应被报 healthy；summary={summary!r}"
    )
    codes = {item["code"] for item in summary["alerts"]}
    assert codes & {
        "data_source_unavailable",
        "state_store_unavailable",
        "trading_module_unavailable",
    }, f"应出现 data_source 类告警；alerts={summary['alerts']!r}"


def test_trading_state_alerts_state_store_only_failure_still_alerts() -> None:
    """state_store 故障 + trading_module 正常仍应告警（不能因 trading_module
    返回数据就把整体标 healthy）。"""
    alerts = TradingStateAlerts(
        state_store=_BrokenStateStore(),
        trading_module=DummyTradingModule(),
        account_alias_getter=lambda: "default",
    )
    summary = alerts.summary()
    assert summary["status"] != "healthy"
    codes = {item["code"] for item in summary["alerts"]}
    assert codes & {"state_store_unavailable", "data_source_unavailable"}, (
        f"state_store 故障必须告警；alerts={summary['alerts']!r}"
    )


# ── P3 回归：monitoring_summary(hours) 必须真实生效 ─────────────────────────


def test_monitoring_summary_records_hours_in_payload() -> None:
    """P3 回归：旧实现 del hours 后无条件返当前快照。修复至少把 hours 反映
    在 payload time_range_hours 字段，让消费方知道窗口意图。
    """
    alerts = TradingStateAlerts(
        state_store=DummyStateStore(),
        trading_module=DummyTradingModule(),
        account_alias_getter=lambda: "default",
    )
    summary_1h = alerts.monitoring_summary(hours=1)
    summary_24h = alerts.monitoring_summary(hours=24)
    assert summary_1h.get("time_range_hours") == 1, (
        f"hours=1 必须被记录到 payload；got {summary_1h!r}"
    )
    assert summary_24h.get("time_range_hours") == 24, (
        f"hours=24 必须被记录到 payload；got {summary_24h!r}"
    )
