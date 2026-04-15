from __future__ import annotations

from pathlib import Path

from src.monitoring.health import HealthMonitor, close_health_monitor, get_health_monitor
from src.monitoring.manager import (
    MonitoringManager,
    close_monitoring_manager,
    get_monitoring_manager,
)


def test_health_report_skips_non_finite_metrics(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("market_data", "data_latency", float("inf"))
    report = monitor.generate_report(hours=1)

    assert report["summary"]["total_metrics"] == 0
    assert report["overall_status"] == "healthy"


def test_cache_hit_rate_no_longer_triggers_alert(tmp_path: Path) -> None:
    # cache_hit_rate 阈值已设为 0（仅作信息指标），即使值为 0 也不触发告警。
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("indicator_calculation", "cache_hit_rate", 0.0)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["indicator_calculation"]["cache_hit_rate"]
    assert metric["status"] == "healthy"


def test_indicator_compute_p99_triggers_warning(tmp_path: Path) -> None:
    # indicator_compute_p99_ms 是替代 cache_hit_rate 的新告警指标。
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("indicator_calculation", "indicator_compute_p99_ms", 800.0)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["indicator_calculation"]["indicator_compute_p99_ms"]
    assert metric["status"] == "warning"


def test_indicator_intrabar_slo_metrics_trigger_alerts(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("indicator_calculation", "intrabar_drop_rate_1m", 7.5)
    monitor.record_metric("indicator_calculation", "intrabar_queue_age_p95_ms", 2600.0)
    monitor.record_metric("indicator_calculation", "intrabar_to_decision_latency_p95_ms", 8000.0)

    report = monitor.generate_report(hours=1)

    assert report["components"]["indicator_calculation"]["intrabar_drop_rate_1m"]["status"] == "critical"
    assert report["components"]["indicator_calculation"]["intrabar_queue_age_p95_ms"]["status"] == "warning"
    assert report["components"]["indicator_calculation"]["intrabar_to_decision_latency_p95_ms"]["status"] == "critical"


def test_health_report_uses_blocking_metrics_for_critical_status(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    monitor.record_metric("market_data", "data_latency", 999.0)
    report = monitor.generate_report(hours=1)

    metric = report["components"]["market_data"]["data_latency"]
    assert metric["status"] == "critical"
    assert metric["overall_impact"] == "blocking"
    assert report["overall_status"] == "critical"


def test_monitoring_manager_lists_registered_components(tmp_path: Path) -> None:
    monitor = HealthMonitor(str(tmp_path / "health.db"))
    manager = MonitoringManager(monitor, check_interval=5)
    manager.register_component("signals", object(), ["status"])
    manager.register_component("market_data", object(), ["data_latency"])

    rows = manager.list_registered_components()

    assert [item["name"] for item in rows] == ["market_data", "signals"]
    assert rows[0]["methods"] == ["data_latency"]
    assert rows[1]["methods"] == ["status"]


def test_monitoring_manager_factory_is_scoped_by_health_monitor(tmp_path: Path) -> None:
    monitor_a = get_health_monitor(str(tmp_path / "a.db"))
    monitor_b = get_health_monitor(str(tmp_path / "b.db"))

    manager_a = get_monitoring_manager(monitor_a, check_interval=5)
    manager_b = get_monitoring_manager(monitor_b, check_interval=5)

    assert manager_a is not manager_b

    close_monitoring_manager(instance=manager_a)
    close_monitoring_manager(instance=manager_b)
    close_health_monitor(instance=monitor_a)
    close_health_monitor(instance=monitor_b)


def test_startup_grace_suppresses_transient_staleness_alert(tmp_path: Path) -> None:
    """启动 grace 期内，economic_calendar_staleness 不应触发 alert。

    场景：启动瞬间 economic_calendar 尚未 refresh，staleness 可能 > critical 阈值，
    但几秒内 refresh 会让它回归正常。grace 期抑制这类瞬态 alert 避免告警历史污染。
    """
    monitor = HealthMonitor(str(tmp_path / "health.db"))
    # builder_phases/monitoring.py 会在生产构建时注入这个阈值；测试需显式配置
    monitor.alerts["economic_calendar_staleness"] = {"warning": 900.0, "critical": 1800.0}

    # 启动瞬间 record 一个超高 staleness 值（77041s，真实观察到的启动瞬态）
    monitor.record_metric(
        "economic_calendar", "economic_calendar_staleness", 77041.0
    )

    # 未触发 alert（被 grace 抑制）
    assert monitor.active_alerts == {}
    # 但 metric 仍被正常记录（供事后审计）
    report = monitor.generate_report(hours=1)
    assert "economic_calendar" in report["components"]


def test_startup_grace_does_not_mask_runtime_data_latency(tmp_path: Path) -> None:
    """data_latency 是运行时数据流问题，不应被 grace 豁免。"""
    monitor = HealthMonitor(str(tmp_path / "health.db"))

    # data_latency 超 critical 阈值（30.0），启动瞬间也应触发 alert
    monitor.record_metric("market_data", "data_latency", 999.0)

    assert "market_data.data_latency" in monitor.active_alerts


def test_startup_grace_expires_after_threshold(tmp_path: Path, monkeypatch) -> None:
    """grace 期过后，豁免的 metric 应正常触发 alert。"""
    monitor = HealthMonitor(str(tmp_path / "health.db"))
    monitor.alerts["economic_calendar_staleness"] = {"warning": 900.0, "critical": 1800.0}

    # 人工把启动时间往前推 70 秒，超出 60s grace 期
    import time as _time
    monkeypatch.setattr(
        monitor, "_started_at_monotonic", _time.monotonic() - 70.0
    )

    monitor.record_metric(
        "economic_calendar", "economic_calendar_staleness", 77041.0
    )

    assert "economic_calendar.economic_calendar_staleness" in monitor.active_alerts
