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
