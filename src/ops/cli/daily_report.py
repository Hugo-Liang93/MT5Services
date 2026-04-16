"""日终对账报告 CLI — 汇总当日交易活动和异常事件。

用法：
    python -m src.ops.cli.daily_report
    python -m src.ops.cli.daily_report --date 2026-04-16
    python -m src.ops.cli.daily_report --host 127.0.0.1 --port 8808

设计原则：纯 HTTP 请求，不启动服务、不导入业务代码。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


def _fetch_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _render_daily_report(
    summary: dict[str, Any],
    executor: dict[str, Any],
    health: dict[str, Any],
    report_date: str,
) -> str:
    lines = [
        "",
        "=" * 64,
        f" DAILY REPORT — {report_date}",
        "=" * 64,
    ]

    # ── 交易汇总 ──
    lines.append("")
    lines.append("── 交易汇总 ──")
    trades = summary.get("trades", [])
    if not trades:
        lines.append("  (无交易记录)")
    else:
        opened = [t for t in trades if t.get("type") == "opened"]
        closed = [t for t in trades if t.get("type") == "closed"]
        lines.append(f"  开仓: {len(opened)} 笔  |  平仓: {len(closed)} 笔")

        total_pnl = sum(float(t.get("profit", 0) or 0) for t in closed)
        if closed:
            lines.append(f"  当日 PnL: {total_pnl:+.2f}")

        for t in trades:
            trade_type = t.get("type", "?")
            ticket = t.get("ticket", "?")
            symbol = t.get("symbol", "?")
            direction = t.get("direction", "?")
            volume = t.get("volume", "?")
            price = t.get("price", "?")
            profit = t.get("profit")
            profit_str = f"  pnl={profit:+.2f}" if profit is not None else ""
            lines.append(
                f"    [{trade_type:>6}] #{ticket} {symbol} {direction} "
                f"vol={volume} @ {price}{profit_str}"
            )

    # ── 信号统计 ──
    lines.append("")
    lines.append("── 信号执行统计 ──")
    received = executor.get("signals_received", 0)
    passed = executor.get("signals_passed", 0)
    blocked = executor.get("signals_blocked", 0)
    exec_count = executor.get("execution_count", 0)
    lines.append(f"  接收: {received}  |  通过: {passed}  |  拒绝: {blocked}")
    lines.append(f"  实际执行: {exec_count}")

    skip_reasons = executor.get("skip_reasons", {})
    if skip_reasons:
        lines.append("  拒绝原因:")
        for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            if count > 0:
                lines.append(f"    {reason}: {count}")

    # ── 熔断器 ──
    cb = executor.get("circuit_breaker", {})
    if cb.get("open"):
        lines.append("")
        lines.append("── ⚠ 熔断器 ──")
        lines.append(f"  状态: OPEN (since {cb.get('circuit_open_at', '?')})")
        lines.append(
            f"  连续失败: {cb.get('consecutive_failures', '?')} / "
            f"{cb.get('max_consecutive_failures', '?')}"
        )

    # ── 健康告警 ──
    active_alerts = health.get("active_alerts", {})
    if active_alerts:
        lines.append("")
        lines.append("── 活跃告警 ──")
        for key, info in active_alerts.items():
            level = info.get("alert_level", "?")
            msg = info.get("message", "")
            lines.append(f"  [{level:>8}] {key}: {msg}")

    # ── 执行质量 ──
    eq = executor.get("execution_quality", {})
    if eq:
        lines.append("")
        lines.append("── 执行质量 ──")
        risk_blocks = eq.get("risk_blocks", 0)
        recovered = eq.get("recovered_from_state", 0)
        slippage_samples = eq.get("slippage_samples", 0)
        avg_slippage = eq.get("avg_slippage_price")
        lines.append(f"  风控拦截: {risk_blocks}")
        lines.append(f"  状态恢复: {recovered}")
        if slippage_samples > 0 and avg_slippage is not None:
            lines.append(f"  滑点: avg={avg_slippage:.2f} ({slippage_samples} samples)")

    lines.append("")
    lines.append("=" * 64)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily trade reconciliation report")
    parser.add_argument("--host", default="127.0.0.1", help="API host")
    parser.add_argument("--port", type=int, default=8808, help="API port")
    parser.add_argument(
        "--date",
        default=None,
        help="报告日期 (YYYY-MM-DD)，默认今天",
    )
    args = parser.parse_args()

    report_date = args.date or date.today().isoformat()
    base = f"http://{args.host}:{args.port}"

    sys.stderr.write(f"Fetching daily report for {report_date}...\n")

    # 1. 交易日报
    try:
        summary_resp = _fetch_json(f"{base}/v1/trade/daily_summary")
        summary = summary_resp.get("data", {})
    except Exception as exc:
        summary = {"error": str(exc)}
        sys.stderr.write(f"  daily_summary failed: {exc}\n")

    # 2. 执行器状态
    try:
        overview_resp = _fetch_json(f"{base}/v1/trade/overview")
        overview = overview_resp.get("data", {})
        executor = overview.get("executor", {})
    except Exception as exc:
        executor = {"error": str(exc)}
        sys.stderr.write(f"  trade/overview failed: {exc}\n")

    # 3. 健康报告（含告警）
    try:
        health_resp = _fetch_json(f"{base}/v1/monitoring/health")
        health = health_resp.get("data", {})
    except Exception as exc:
        health = {"error": str(exc)}
        sys.stderr.write(f"  health failed: {exc}\n")

    output = _render_daily_report(summary, executor, health, report_date)
    print(output)


if __name__ == "__main__":
    main()
