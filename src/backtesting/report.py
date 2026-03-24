"""回测报告生成：终端表格输出和 JSON 序列化。"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .models import BacktestMetrics, BacktestResult


def format_summary(result: BacktestResult) -> str:
    """生成终端友好的回测摘要报告。"""
    m = result.metrics
    lines = [
        "",
        f"{'=' * 60}",
        f"  回测报告  |  Run ID: {result.run_id}",
        f"{'=' * 60}",
        f"  品种/周期: {result.config.symbol} / {result.config.timeframe}",
        f"  回测区间: {result.config.start_time.strftime('%Y-%m-%d')} ~ {result.config.end_time.strftime('%Y-%m-%d')}",
        f"  初始资金: {result.config.initial_balance:,.2f}",
        f"{'─' * 60}",
        f"  总交易数: {m.total_trades}",
        f"  盈利/亏损: {m.winning_trades} / {m.losing_trades}",
        f"  胜率:     {m.win_rate * 100:.2f}%",
        f"  总盈亏:   {m.total_pnl:+,.2f} ({m.total_pnl_pct:+.2f}%)",
        f"{'─' * 60}",
        f"  期望值:     {m.expectancy:+.4f}",
        f"  盈亏比:     {m.profit_factor:.4f}",
        f"  Sharpe:     {m.sharpe_ratio:.4f}",
        f"  Sortino:    {m.sortino_ratio:.4f}",
        f"  最大回撤:   {m.max_drawdown * 100:.2f}% (持续 {m.max_drawdown_duration} bars)",
        f"  平均盈利:   {m.avg_win:+.4f}",
        f"  平均亏损:   {m.avg_loss:.4f}",
        f"  平均持仓:   {m.avg_bars_held:.1f} bars",
        f"  最大连胜:   {m.max_consecutive_wins}",
        f"  最大连败:   {m.max_consecutive_losses}",
        f"{'─' * 60}",
    ]

    # 过滤器统计
    if result.filter_stats:
        lines.extend(_format_filter_stats(result.filter_stats))

    # 按策略分组
    if result.metrics_by_strategy:
        lines.append("  按策略分组:")
        lines.append(
            f"  {'策略':<30} {'交易数':>6} {'胜率':>8} {'盈亏':>12} {'Sharpe':>8}"
        )
        for name, sm in sorted(
            result.metrics_by_strategy.items(),
            key=lambda x: x[1].total_pnl,
            reverse=True,
        ):
            lines.append(
                f"  {name:<30} {sm.total_trades:>6} "
                f"{sm.win_rate * 100:>7.1f}% "
                f"{sm.total_pnl:>+11.2f} "
                f"{sm.sharpe_ratio:>7.4f}"
            )
        lines.append(f"{'─' * 60}")

    # 按 Regime 分组
    if result.metrics_by_regime:
        lines.append("  按市场状态分组:")
        lines.append(
            f"  {'Regime':<20} {'交易数':>6} {'胜率':>8} {'盈亏':>12}"
        )
        for name, rm in sorted(
            result.metrics_by_regime.items(),
            key=lambda x: x[1].total_trades,
            reverse=True,
        ):
            lines.append(
                f"  {name:<20} {rm.total_trades:>6} "
                f"{rm.win_rate * 100:>7.1f}% "
                f"{rm.total_pnl:>+11.2f}"
            )
        lines.append(f"{'─' * 60}")

    # 信号评估统计
    if result.signal_evaluations:
        lines.extend(_format_signal_eval_stats(result))

    # 参数信息
    if result.param_set:
        lines.append("  参数覆盖:")
        for k, v in result.param_set.items():
            lines.append(f"    {k} = {v}")
        lines.append(f"{'─' * 60}")

    lines.append(
        f"  耗时: {(result.completed_at - result.started_at).total_seconds():.1f}s"
    )
    lines.append(f"{'=' * 60}")
    lines.append("")
    return "\n".join(lines)


def _format_filter_stats(stats: Dict[str, Any]) -> List[str]:
    """格式化过滤器统计信息。"""
    lines = [
        "  过滤器统计:",
        f"    总评估 bars: {stats.get('total_bars_evaluated', 0)}",
        f"    被过滤 bars: {stats.get('total_bars_rejected', 0)}",
        f"    通过率:      {stats.get('pass_rate', 0) * 100:.1f}%",
    ]
    rejections = stats.get("rejections_by_filter", {})
    if rejections:
        lines.append("    按过滤器拒绝统计:")
        for filter_name, count in sorted(
            rejections.items(), key=lambda x: x[1], reverse=True
        ):
            lines.append(f"      {filter_name:<25} {count:>6} 次")
    lines.append(f"{'─' * 60}")
    return lines


def _format_signal_eval_stats(result: BacktestResult) -> List[str]:
    """格式化信号评估统计。"""
    evals = result.signal_evaluations or []
    if not evals:
        return []

    total = len(evals)
    filtered = sum(1 for e in evals if e.filtered)
    actionable = [e for e in evals if e.direction in ("buy", "sell") and not e.filtered]
    won = sum(1 for e in actionable if e.won is True)
    lost = sum(1 for e in actionable if e.won is False)
    win_rate = won / (won + lost) * 100 if (won + lost) > 0 else 0.0

    lines = [
        "  信号评估统计:",
        f"    总评估次数: {total}",
        f"    被过滤:     {filtered}",
        f"    有方向信号: {len(actionable)}",
        f"    信号胜率:   {win_rate:.1f}% ({won}W / {lost}L)",
    ]

    # 按策略的信号质量
    by_strategy: Dict[str, Dict[str, int]] = {}
    for e in actionable:
        s = by_strategy.setdefault(e.strategy, {"won": 0, "lost": 0, "total": 0})
        s["total"] += 1
        if e.won is True:
            s["won"] += 1
        elif e.won is False:
            s["lost"] += 1

    if by_strategy:
        lines.append(f"    {'策略':<30} {'信号数':>6} {'信号胜率':>8}")
        for name, s in sorted(
            by_strategy.items(),
            key=lambda x: x[1]["total"],
            reverse=True,
        ):
            wr = s["won"] / (s["won"] + s["lost"]) * 100 if (s["won"] + s["lost"]) > 0 else 0.0
            lines.append(f"    {name:<30} {s['total']:>6} {wr:>7.1f}%")

    lines.append(f"{'─' * 60}")
    return lines


def format_optimization_summary(results: List[BacktestResult]) -> str:
    """生成参数优化对比摘要。"""
    if not results:
        return "无回测结果。"

    lines = [
        "",
        f"{'=' * 80}",
        f"  参数优化结果  |  共 {len(results)} 组参数",
        f"{'=' * 80}",
        f"  {'排名':>4} {'Sharpe':>8} {'胜率':>8} {'盈亏':>12} {'交易数':>6} {'最大回撤':>8} {'参数':<30}",
        f"{'─' * 80}",
    ]

    for i, r in enumerate(results[:20]):  # 最多显示前 20 名
        m = r.metrics
        params_str = ", ".join(f"{k}={v}" for k, v in r.param_set.items())
        if len(params_str) > 40:
            params_str = params_str[:37] + "..."
        lines.append(
            f"  {i + 1:>4} "
            f"{m.sharpe_ratio:>7.4f} "
            f"{m.win_rate * 100:>7.1f}% "
            f"{m.total_pnl:>+11.2f} "
            f"{m.total_trades:>6} "
            f"{m.max_drawdown * 100:>7.2f}% "
            f"{params_str}"
        )

    lines.append(f"{'=' * 80}")
    lines.append("")
    return "\n".join(lines)


def result_to_json(result: BacktestResult) -> str:
    """将回测结果序列化为 JSON。"""
    import math

    def _json_default(obj: Any) -> Any:
        if isinstance(obj, float):
            if math.isinf(obj):
                return 999.99 if obj > 0 else -999.99
            if math.isnan(obj):
                return None
        return str(obj)

    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=_json_default)


def format_metrics_table(metrics: BacktestMetrics) -> Dict[str, Any]:
    """将 BacktestMetrics 转为字典（API 响应用）。"""
    from dataclasses import asdict

    return asdict(metrics)
