"""MonitoringManager mode-aware probe gate 回归测试。

修复背景：EOD 后 mode_controller 切到 ingest_only，trading 子系统（signal_queue
/ pending_entry / position_manager 等）合法停止，但 MonitoringManager 仍按
"全模式应该全活" 探活，每 5 秒持续误报 WalSignalQueue closed / pending_runtime_down
/ reconciliation lag，errors.log 累积上千 noise。

修复后：register_component(..., trading_only=True) + is_trading_active_fn 注入。
gate=False 时所有 probe 正常运行（向后兼容）；gate=True 且 fn 返回 False 时
trading_only 组件 dispatch 阶段 skip。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.monitoring.manager import MonitoringManager


class _StubHealthMonitor:
    def __init__(self) -> None:
        self.alerts: dict = {}

    def record_metric(self, *args, **kwargs) -> None:
        pass

    def generate_report(self, hours: int = 1) -> dict:
        return {"overall_status": "healthy"}

    def cleanup_old_data(self, *args, **kwargs) -> None:
        pass

    def check_queue_stats(self, *args, **kwargs) -> None:
        pass

    def check_cache_stats(self, *args, **kwargs) -> None:
        pass


def _drive_one_loop(manager: MonitoringManager) -> None:
    """触发一次 _monitoring_loop 的 dispatch 后立即停止。"""
    manager.check_interval = 0  # 无等待
    manager.start()
    time.sleep(0.05)
    manager._stop.set()
    if manager._thread is not None:
        manager._thread.join(timeout=1)


def test_trading_only_component_skipped_when_trading_inactive() -> None:
    """ingest_only 模式下 trading_only 组件不应被 dispatch。"""
    health = _StubHealthMonitor()
    trading_active = False
    manager = MonitoringManager(
        health, check_interval=0, is_trading_active_fn=lambda: trading_active
    )

    trading_obj = MagicMock()
    trading_obj.status.return_value = {"running": True}
    manager.register_component("signals", trading_obj, ["status"], trading_only=True)

    ingest_obj = MagicMock()
    ingest_obj.queue_stats.return_value = {"size": 0}
    manager.register_component("data_ingestion", ingest_obj, ["queue_stats"])

    _drive_one_loop(manager)

    # trading_only 组件未被 probe（status 调用 = 0）
    trading_obj.status.assert_not_called()


def test_trading_only_component_probed_when_trading_active() -> None:
    """FULL 模式下 trading_only 组件正常被 probe。"""
    health = _StubHealthMonitor()
    manager = MonitoringManager(
        health, check_interval=0, is_trading_active_fn=lambda: True
    )

    trading_obj = MagicMock()
    trading_obj.status.return_value = {"running": True}
    manager.register_component("signals", trading_obj, ["status"], trading_only=True)

    _drive_one_loop(manager)

    trading_obj.status.assert_called()


def test_no_gate_when_fn_not_injected_backwards_compat() -> None:
    """未注入 is_trading_active_fn 时 trading_only flag 被忽略（向后兼容）。"""
    health = _StubHealthMonitor()
    manager = MonitoringManager(health, check_interval=0)  # 无 fn

    trading_obj = MagicMock()
    trading_obj.status.return_value = {"running": True}
    manager.register_component("signals", trading_obj, ["status"], trading_only=True)

    _drive_one_loop(manager)

    # 向后兼容：无 fn 时 probe 仍执行
    trading_obj.status.assert_called()


def test_non_trading_components_always_probed() -> None:
    """trading_only=False 的组件不受 mode gate 影响。

    queue_stats method 实际由 health_monitor.check_queue_stats 处理（不是直接
    调 component.queue_stats），所以验证点是 check_queue_stats 被调用。
    """
    health = _StubHealthMonitor()
    health.check_queue_stats = MagicMock()
    manager = MonitoringManager(
        health, check_interval=0, is_trading_active_fn=lambda: False
    )

    ingest_obj = MagicMock()
    ingest_obj.queue_stats.return_value = {"size": 0}
    manager.register_component(
        "data_ingestion", ingest_obj, ["queue_stats"]  # 默认 trading_only=False
    )

    _drive_one_loop(manager)

    # mode 非 active 但组件不是 trading_only → 仍被分发
    health.check_queue_stats.assert_called()


def test_status_probe_records_consumer_progress_health_metrics() -> None:
    """consumer status 不只是 heartbeat，还要落 stalled/errors/takeover 指标。"""
    health = _StubHealthMonitor()
    health.record_metric = MagicMock()
    manager = MonitoringManager(
        health, check_interval=0, is_trading_active_fn=lambda: True
    )

    consumer = MagicMock()
    consumer.status.return_value = {
        "running": True,
        "stalled": True,
        "consecutive_errors": 4,
        "in_flight_duration_seconds": 61.0,
        "total_takeovers": 2,
    }
    manager.register_component(
        "execution_intent_consumer",
        consumer,
        ["status"],
        trading_only=True,
    )

    _drive_one_loop(manager)

    calls = [args[:3] for args, _kwargs in health.record_metric.call_args_list]
    assert ("execution_intent_consumer", "consumer_stalled", 1.0) in calls
    assert ("execution_intent_consumer", "consumer_consecutive_errors", 4.0) in calls
    assert ("execution_intent_consumer", "consumer_in_flight_duration_seconds", 61.0) in calls
    assert ("execution_intent_consumer", "consumer_lease_takeovers_total", 2.0) in calls
