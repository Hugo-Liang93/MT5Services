"""Regression tests for src/ops/cli/daily_report.py.

P2 历史 bug 两处：

1. _render_daily_report 仍按旧 executor schema 渲染：读 flat
   signals_received/signals_passed/signals_blocked + nested circuit_breaker.{open}，
   但 /v1/trade/control 现行 schema 是 nested signals.{received,passed,blocked,skip_reasons}
   + 顶层 circuit_open / consecutive_failures。
   → 接收/通过/拒绝 永远显示 0；熔断器告警永远不显示。

2. main() 的 --date 只写进标题，不传给 /v1/trade/daily_summary。
   → 用户生成的"历史日报"内容仍是当日数据。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from src.ops.cli import daily_report

# ---------------------------------------------------------------------------
# _render_daily_report — schema mismatch regression
# ---------------------------------------------------------------------------


def _new_schema_executor() -> dict[str, Any]:
    """trade_executor_summary() 真实返回结构（参 src/readmodels/runtime.py）。"""
    return {
        "status": "ok",
        "configured": True,
        "armed": True,
        "running": True,
        "enabled": True,
        "circuit_open": False,
        "consecutive_failures": 0,
        "execution_count": 17,
        "last_execution_at": "2026-04-26T10:00:00+00:00",
        "last_error": None,
        "last_risk_block": None,
        "signals": {
            "received": 50,
            "passed": 30,
            "blocked": 20,
            "skip_reasons": {"daily_loss_limit": 8, "spread_too_wide": 12},
            "by_timeframe": {},
        },
        "execution_quality": {},
        "config": {"max_consecutive_failures": 3},
        "execution_gate": {},
    }


def test_render_signals_uses_nested_schema_not_flat() -> None:
    """回归：旧实现读 executor.signals_received（不存在）→ 永远显示 0。

    现行 schema 是 executor.signals.{received,passed,blocked}。
    """
    executor = _new_schema_executor()
    output = daily_report._render_daily_report(
        summary={"trades": []},
        executor=executor,
        health={},
        report_date="2026-04-26",
    )
    # 新 schema 下数字应真实呈现
    assert (
        "接收: 50" in output
    ), f"signals.received=50 必须呈现，旧实现读 signals_received → 0；输出:\n{output}"
    assert "通过: 30" in output
    assert "拒绝: 20" in output
    assert "实际执行: 17" in output


def test_render_skip_reasons_uses_nested_signals_path() -> None:
    """回归：旧实现读 executor.skip_reasons 顶层（已迁入 signals.skip_reasons）。"""
    executor = _new_schema_executor()
    output = daily_report._render_daily_report(
        summary={"trades": []},
        executor=executor,
        health={},
        report_date="2026-04-26",
    )
    assert "daily_loss_limit: 8" in output
    assert "spread_too_wide: 12" in output


def test_render_circuit_open_uses_top_level_field() -> None:
    """回归：旧实现读 executor.circuit_breaker.open（嵌套字段已不存在）→
    熔断告警永远不显示。现行 schema 是顶层 circuit_open。
    """
    executor = _new_schema_executor()
    executor["circuit_open"] = True
    executor["consecutive_failures"] = 5

    output = daily_report._render_daily_report(
        summary={"trades": []},
        executor=executor,
        health={},
        report_date="2026-04-26",
    )
    assert "熔断器" in output
    assert "OPEN" in output
    assert "5" in output  # consecutive_failures


def test_render_circuit_closed_does_not_show_breaker_section() -> None:
    """circuit_open=False 时不应显示熔断段（避免噪声）。"""
    executor = _new_schema_executor()
    output = daily_report._render_daily_report(
        summary={"trades": []},
        executor=executor,
        health={},
        report_date="2026-04-26",
    )
    assert "OPEN" not in output


# ---------------------------------------------------------------------------
# main() --date threading regression
# ---------------------------------------------------------------------------


def test_main_threads_date_arg_into_daily_summary_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """回归：旧实现 args.date 只写标题，不传给 /v1/trade/daily_summary。

    断言 _fetch_json 被以 ?date=2024-01-15 参数调用一次。
    """
    captured: list[str] = []

    def fake_fetch(url: str, timeout: float = 5.0) -> dict[str, Any]:
        captured.append(url)
        if "/trade/daily_summary" in url:
            return {"data": {"trades": []}}
        if "/trade/control" in url:
            return {"data": {"executor": _new_schema_executor()}}
        if "/monitoring/health" in url:
            return {"data": {"active_alerts": {}}}
        return {"data": {}}

    monkeypatch.setattr(daily_report, "_fetch_json", fake_fetch)
    monkeypatch.setattr(
        "sys.argv",
        [
            "daily_report",
            "--host",
            "localhost",
            "--port",
            "8808",
            "--date",
            "2024-01-15",
        ],
    )

    daily_report.main()

    summary_calls = [u for u in captured if "/v1/trade/daily_summary" in u]
    assert summary_calls, "应调用 /v1/trade/daily_summary"
    assert any(
        "date=2024-01-15" in u for u in summary_calls
    ), f"--date 必须拼进 /v1/trade/daily_summary URL，实际 calls: {summary_calls!r}"


def test_main_omits_date_query_when_not_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 --date 时不应附 date= query（避免误传 today 字符串干扰路由默认行为）。"""
    captured: list[str] = []

    def fake_fetch(url: str, timeout: float = 5.0) -> dict[str, Any]:
        captured.append(url)
        if "/trade/daily_summary" in url:
            return {"data": {"trades": []}}
        if "/trade/control" in url:
            return {"data": {"executor": _new_schema_executor()}}
        if "/monitoring/health" in url:
            return {"data": {"active_alerts": {}}}
        return {"data": {}}

    monkeypatch.setattr(daily_report, "_fetch_json", fake_fetch)
    monkeypatch.setattr("sys.argv", ["daily_report"])

    daily_report.main()

    summary_calls = [u for u in captured if "/v1/trade/daily_summary" in u]
    assert summary_calls
    assert not any(
        "date=" in u for u in summary_calls
    ), f"无 --date 时不应拼 date= query；实际 {summary_calls!r}"
