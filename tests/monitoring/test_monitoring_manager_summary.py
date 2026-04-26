from __future__ import annotations

from src.monitoring.manager import MonitoringManager


# ── §0u P2 回归：MonitoringManager._summary_status_value 把 failed 行漂白 ──


def test_summary_status_value_treats_failed_row_without_count_as_failure() -> None:
    """P2 §0u 回归：旧实现 row.get('count', 0)，TradingStateAlerts.summary() 产出
    的 summary 行只有 code/status/severity/message，没有 count → 旧 helper 算成
    0 → 返回 1.0（healthy）→ MonitoringManager 把已经 failed 的状态记录成健康指标，
    health ring buffer / report 都被污染成假阳性。
    """
    summary_payload = {
        "summary": [
            {
                "code": "trading_module_unavailable",
                "status": "failed",
                "severity": "critical",
                "message": "MT5 不可用",
            }
        ]
    }
    value = MonitoringManager._summary_status_value(summary_payload)
    assert value < 1.0, (
        f"failed 行存在必须返回 <1.0（降级值）；got {value}"
    )


def test_summary_status_value_returns_healthy_when_no_failed_rows() -> None:
    """无 failed 行时正常返 1.0，确认契约对称。"""
    payload = {
        "summary": [
            {"code": "x", "status": "ok", "severity": "info", "message": ""}
        ]
    }
    assert MonitoringManager._summary_status_value(payload) == 1.0


def test_summary_status_value_handles_empty_summary() -> None:
    assert MonitoringManager._summary_status_value({}) == 1.0
    assert MonitoringManager._summary_status_value({"summary": []}) == 1.0


# ── §0u P2 回归：execution_failure_rate 在全失败场景静默 ──


class _StubHealthMonitor:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, float]] = []

    def record_metric(self, component, metric, value, *args, **kwargs):
        self.records.append((component, metric, float(value)))


class _StubExecutor:
    def __init__(self, exec_count: int, failed_count: int) -> None:
        self._exec_count = exec_count
        self._failed_count = failed_count

    def status(self) -> dict:
        return {
            "execution_count": self._exec_count,
            "skip_reasons": {"execution_failed": self._failed_count},
            "execution_quality": {"queue_overflows": 0},
        }


def test_check_execution_quality_records_metric_when_all_dispatches_failed() -> None:
    """P2 §0u 回归：旧实现 `if total > 0` 用 success-only 计数当分母——
    全部 dispatch 失败的极端场景 (execution_count=0 + execution_failed>0) 下，
    execution_failure_rate 一条都不写入，监控变成静默空白。
    """
    from src.monitoring.manager import MonitoringManager

    health = _StubHealthMonitor()
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.health_monitor = health  # type: ignore[attr-defined]

    executor = _StubExecutor(exec_count=0, failed_count=5)
    manager._check_execution_quality(executor, "trading_executor")

    matches = [r for r in health.records if r[1] == "execution_failure_rate"]
    assert matches, (
        f"全失败场景必须仍记录 execution_failure_rate；records={health.records!r}"
    )
    component, metric, value = matches[0]
    assert value == 1.0, f"全失败场景 failure_rate 必须 = 1.0；got {value}"


def test_check_execution_quality_records_correct_rate_with_partial_failures() -> None:
    """部分失败：3 成功 + 1 失败 → rate = 1/(3+1) = 0.25"""
    from src.monitoring.manager import MonitoringManager

    health = _StubHealthMonitor()
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.health_monitor = health  # type: ignore[attr-defined]

    executor = _StubExecutor(exec_count=3, failed_count=1)
    manager._check_execution_quality(executor, "trading_executor")

    matches = [r for r in health.records if r[1] == "execution_failure_rate"]
    assert matches, "至少要有一条 execution_failure_rate"
    assert matches[0][2] == 0.25, f"got {matches[0][2]}"


# ── §0w P2 回归：pending health 检查必须覆盖 fill worker ──


class _FakeThread:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _StubPendingMgr:
    def __init__(self, monitor_alive: bool, fill_alive: bool) -> None:
        self._monitor_thread = _FakeThread(monitor_alive)
        self._fill_worker_thread = _FakeThread(fill_alive)

    def status(self) -> dict:
        return {
            "active_count": 0,
            "stats": {
                "total_filled": 0,
                "total_expired": 0,
                "total_submitted": 0,
                "fill_rate": 1.0,
            },
        }

    def is_running(self) -> bool:
        return self._monitor_thread.is_alive() and self._fill_worker_thread.is_alive()


def test_check_pending_entry_records_runtime_down_when_fill_worker_dies() -> None:
    """P2 §0w 回归：旧实现只看 _monitor_thread → fill worker 挂掉时仍报 monitor
    存活 = 1.0，把执行链断掉的状态误报成健康。新实现必须基于 is_running()
    （covers monitor + fill worker）写入聚合 pending_runtime_down 指标，且在
    fill worker 死时该值 = 1.0（down）。
    """
    from src.monitoring.manager import MonitoringManager

    health = _StubHealthMonitor()
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.health_monitor = health  # type: ignore[attr-defined]

    pending = _StubPendingMgr(monitor_alive=True, fill_alive=False)
    manager._check_pending_entry(pending, "pending_mgr")

    metric_map = {(c, m): v for (c, m, v) in health.records}
    runtime_down = metric_map.get(("pending_mgr", "pending_runtime_down"))
    assert runtime_down == 1.0, (
        f"fill worker 挂掉时 pending_runtime_down 必须 = 1.0；"
        f"got {runtime_down!r}, all records={health.records!r}"
    )


def test_check_pending_entry_records_runtime_down_zero_when_both_alive() -> None:
    """两个线程都活着 → pending_runtime_down = 0（健康）。"""
    from src.monitoring.manager import MonitoringManager

    health = _StubHealthMonitor()
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.health_monitor = health  # type: ignore[attr-defined]

    pending = _StubPendingMgr(monitor_alive=True, fill_alive=True)
    manager._check_pending_entry(pending, "pending_mgr")

    metric_map = {(c, m): v for (c, m, v) in health.records}
    assert metric_map.get(("pending_mgr", "pending_runtime_down")) == 0.0


# ── §0w P2 回归：pending_runtime_down 必须能触发 critical alert ──


def test_pending_runtime_down_metric_triggers_critical_alert(tmp_path) -> None:
    """P2 §0w 回归：旧实现 manager 写 pending_monitor_alive 但 HealthMonitor
    没有这个 metric 的 threshold → 即使值为 0（线程死）generate_report 也只显示
    status='healthy'，overall_status 仍 healthy。
    新指标 pending_runtime_down 必须有 threshold 让 0/1 转 critical。
    """
    from src.monitoring.health import HealthMonitor

    monitor = HealthMonitor(str(tmp_path / "pending_runtime.db"))
    monitor.record_metric("pending_mgr", "pending_runtime_down", 1.0)
    report = monitor.generate_report(hours=1)
    metric = report["components"]["pending_mgr"]["pending_runtime_down"]
    assert metric["status"] == "critical", (
        f"pending_runtime_down=1.0 必须触发 critical；got status={metric['status']!r} "
        f"alert_level={metric['alert_level']!r}"
    )
    assert metric["overall_impact"] == "blocking", (
        "pending fill chain 断 = 阻断指标"
    )
    assert report["overall_status"] == "critical", (
        f"挂单链路死必须把 overall_status 抬到 critical；got {report['overall_status']!r}"
    )
