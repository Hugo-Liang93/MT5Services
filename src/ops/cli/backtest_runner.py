"""回测运行器 — 本地直接执行回测并输出结构化摘要。

用法：
    python -m src.ops.cli.backtest_runner --tf H1 --start 2025-12-30 --end 2026-03-30
    python -m src.ops.cli.backtest_runner --tf H1,M30,M15,M5 --start 2025-12-30 --end 2026-03-30
    python -m src.ops.cli.backtest_runner --tf M15 --template regime_detail
    python -m src.ops.cli.backtest_runner --tf H1 --monte-carlo
    python -m src.ops.cli.backtest_runner --tf H1,M30,M15 --compare
    python -m src.ops.cli.backtest_runner --tf H1 --template custom --custom-template '...'

摘要模板：
    default       — 总体指标 + 退出原因 + 策略排名 + regime 分布
    strategy_only — 仅策略排名
    regime_detail — regime × strategy 交叉分析
    exit_analysis — 退出原因深度分析（含 SL/TP 分布）
    minimal       — 仅核心指标（1 行）
    custom        — 通过 --custom-template 传入 Python format string

所有日志静默，仅输出摘要到 stdout。供 Claude Code 通过单次 Bash 调用获取结果。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _run_single(
    tf: str,
    start: str,
    end: str,
    *,
    config_overrides: Optional[Dict[str, Any]] = None,
    strategy_names: Optional[List[str]] = None,
) -> dict:
    """执行单个 TF 回测，返回结构化结果 dict。

    所有参数从 backtest.ini + signal.ini 自动加载，
    仅 config_overrides 中的值覆盖。
    """
    from src.backtesting.component_factory import (
        _load_signal_config_snapshot,
        build_backtest_components,
    )
    from src.backtesting.config import get_backtest_defaults
    from src.backtesting.engine import BacktestEngine
    from src.backtesting.models import BacktestConfig

    # 1. 加载 backtest.ini 默认值（含自动继承 signal.ini + risk.ini）
    #    过滤掉 optimizer 专用字段（不属于 BacktestConfig）
    _OPTIMIZER_ONLY_KEYS = {"search_mode", "max_combinations", "sort_metric"}
    defaults = {
        k: v for k, v in get_backtest_defaults().items() if k not in _OPTIMIZER_ONLY_KEYS
    }

    # 2. 从 signal.ini 加载 per-strategy session/timeframe 配置
    signal_config = _load_signal_config_snapshot()
    strategy_sessions: dict = {}
    for name, sess_list in getattr(signal_config, "strategy_sessions", {}).items():
        if sess_list:
            strategy_sessions[name] = (
                list(sess_list) if not isinstance(sess_list, list) else sess_list
            )
    strategy_timeframes: dict = {}
    for name, tf_list in getattr(signal_config, "strategy_timeframes", {}).items():
        if tf_list:
            strategy_timeframes[name] = (
                list(tf_list) if not isinstance(tf_list, list) else tf_list
            )

    # 3. 合并：defaults < signal.ini 出场参数 < CLI overrides
    merged = dict(defaults)
    merged["trailing_tp_enabled"] = bool(
        getattr(signal_config, "trailing_tp_enabled", True)
    )
    merged["trailing_tp_activation_atr"] = float(
        getattr(signal_config, "trailing_tp_activation_atr", 1.2)
    )
    merged["trailing_tp_trail_atr"] = float(
        getattr(signal_config, "trailing_tp_trail_atr", 0.6)
    )
    if config_overrides:
        merged.update(config_overrides)

    # 4. 构建 BacktestConfig（from_flat 自动路由到嵌套子配置）
    config = BacktestConfig.from_flat(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        **merged,
    )

    # 5. 构建组件并运行
    components = build_backtest_components(strategy_names=strategy_names)
    engine = BacktestEngine(
        config=config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        voting_engine=components.get("voting_engine"),
        voting_group_engines=components.get("voting_group_engines"),
        performance_tracker=components.get("performance_tracker"),
    )
    result = engine.run()
    m = result.metrics
    trades = result.trades

    # 6. 构建结构化摘要
    exit_stats: Dict[str, Dict[str, Any]] = {}
    strat_stats: Dict[str, Dict[str, Any]] = {}
    regime_stats: Dict[str, Dict[str, Any]] = {}
    regime_strategy: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for t in trades:
        pnl = getattr(t, "pnl", 0) or 0
        strategy = getattr(t, "strategy", "?")
        regime = getattr(t, "regime", "unknown")
        exit_reason = getattr(t, "close_reason", getattr(t, "exit_reason", "unknown"))
        bars_held = getattr(t, "bars_held", 0) or 0

        # exit
        if exit_reason not in exit_stats:
            exit_stats[exit_reason] = {"n": 0, "pnl": 0.0, "bars_held_sum": 0}
        exit_stats[exit_reason]["n"] += 1
        exit_stats[exit_reason]["pnl"] += pnl
        exit_stats[exit_reason]["bars_held_sum"] += bars_held

        # strategy
        if strategy not in strat_stats:
            strat_stats[strategy] = {"n": 0, "w": 0, "pnl": 0.0}
        strat_stats[strategy]["n"] += 1
        strat_stats[strategy]["pnl"] += pnl
        if pnl > 0:
            strat_stats[strategy]["w"] += 1

        # regime
        if regime not in regime_stats:
            regime_stats[regime] = {"n": 0, "w": 0, "pnl": 0.0}
        regime_stats[regime]["n"] += 1
        regime_stats[regime]["pnl"] += pnl
        if pnl > 0:
            regime_stats[regime]["w"] += 1

        # regime × strategy
        if regime not in regime_strategy:
            regime_strategy[regime] = {}
        if strategy not in regime_strategy[regime]:
            regime_strategy[regime][strategy] = {"n": 0, "w": 0, "pnl": 0.0}
        regime_strategy[regime][strategy]["n"] += 1
        regime_strategy[regime][strategy]["pnl"] += pnl
        if pnl > 0:
            regime_strategy[regime][strategy]["w"] += 1

    # 7. 构建 config 摘要（回测 vs 实盘对比用）
    config_summary = {
        "initial_balance": config.initial_balance,
        "min_confidence": config.confidence.min_confidence,
        "commission_per_lot": config.risk.commission_per_lot,
        "slippage_points": config.risk.slippage_points,
        "max_positions": config.risk.max_positions,
        "trailing_tp": config.trailing_tp.enabled,
        "trailing_tp_activation_atr": config.trailing_tp.activation_atr,
        "trailing_tp_trail_atr": config.trailing_tp.trail_atr,
        "circuit_breaker": config.circuit_breaker.enabled,
        "filters_enabled": config.filters.enabled,
        "session_filter": config.filters.session_enabled,
        "allowed_sessions": config.filters.allowed_sessions,
        "pending_entry": config.pending_entry.enabled,
        "risk_percent": config.position.risk_percent,
        "regime_tp_trending": config.position.regime_tp_trending,
        "regime_sl_trending": config.position.regime_sl_trending,
    }

    return {
        "tf": tf,
        "start": start,
        "end": end,
        "run_id": result.run_id,
        "config_summary": config_summary,
        "metrics": {
            "trades": m.total_trades,
            "win_rate": round(m.win_rate * 100, 1),
            "pnl": round(m.total_pnl, 2),
            "pf": round(m.profit_factor, 3),
            "sharpe": round(m.sharpe_ratio, 3),
            "sortino": round(m.sortino_ratio, 3),
            "calmar": round(m.calmar_ratio, 3),
            "max_dd": round(m.max_drawdown * 100, 2),
            "max_dd_bars": m.max_drawdown_duration,
            "avg_win": round(m.avg_win, 2),
            "avg_loss": round(m.avg_loss, 2),
            "expectancy": round(m.expectancy, 2),
            "win_loss_ratio": (
                round(m.avg_win / m.avg_loss, 2) if m.avg_loss > 0 else 0
            ),
            "avg_bars_held": round(m.avg_bars_held, 1),
            "max_consec_wins": m.max_consecutive_wins,
            "max_consec_losses": m.max_consecutive_losses,
        },
        "exit_stats": exit_stats,
        "strategy_stats": strat_stats,
        "regime_stats": regime_stats,
        "regime_strategy": regime_strategy,
        "monte_carlo": result.monte_carlo_result,
        "filter_stats": result.filter_stats,
        "execution_summary": result.execution_summary,
        "validation_decision": (
            result.validation_decision.to_dict()
            if result.validation_decision is not None
            else None
        ),
        "_raw_result": result,
    }


# ── 摘要模板渲染 ────────────────────────────────────────────────────


def _render_default(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{'='*70}",
        f" {data['tf']} XAUUSD ({data['start']} ~ {data['end']})",
        f"{'='*70}",
        f"Trades:{m['trades']}  WR:{m['win_rate']}%  PnL:{m['pnl']}  PF:{m['pf']}  Sharpe:{m['sharpe']}",
        f"Sortino:{m['sortino']}  Calmar:{m['calmar']}  MaxDD:{m['max_dd']}% ({m['max_dd_bars']}bars)",
        f"AvgWin:{m['avg_win']}  AvgLoss:{m['avg_loss']}  W/L:{m['win_loss_ratio']}  Exp:{m['expectancy']}",
        f"AvgBarsHeld:{m['avg_bars_held']}  MaxConsecWin:{m['max_consec_wins']}  MaxConsecLoss:{m['max_consec_losses']}",
    ]

    # Config 摘要
    cfg = data.get("config_summary", {})
    if cfg:
        lines.append("--- Config ---")
        lines.append(
            f"  Balance:{cfg.get('initial_balance')}  MinConf:{cfg.get('min_confidence')}"
            f"  Commission:{cfg.get('commission_per_lot')}/lot  Slippage:{cfg.get('slippage_points')}pts"
        )
        lines.append(
            f"  MaxPos:{cfg.get('max_positions')}  Risk:{cfg.get('risk_percent')}%"
            f"  TrailingTP:{cfg.get('trailing_tp')}  CircuitBreaker:{cfg.get('circuit_breaker')}"
        )

    lines.append("--- Exit Reasons ---")
    for reason, d in sorted(
        data["exit_stats"].items(), key=lambda x: x[1]["n"], reverse=True
    ):
        avg_bars = d["bars_held_sum"] / d["n"] if d["n"] > 0 else 0
        lines.append(
            f"  {reason:<20} {d['n']:>4}  PnL:{d['pnl']:>+10.2f}  AvgBars:{avg_bars:.1f}"
        )
    lines.append("--- Per-Strategy (by PnL) ---")
    lines.append(f"  {'Strategy':<28} {'Trd':>4} {'Win':>4} {'WR%':>6} {'PnL':>10}")
    for name, s in sorted(
        data["strategy_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True
    ):
        wr = s["w"] / s["n"] * 100 if s["n"] > 0 else 0
        lines.append(
            f"  {name:<28} {s['n']:>4} {s['w']:>4} {wr:>5.1f}% {s['pnl']:>+10.2f}"
        )
    lines.append("--- Regime ---")
    for r, d in sorted(
        data["regime_stats"].items(), key=lambda x: x[1]["n"], reverse=True
    ):
        wr = d["w"] / d["n"] * 100 if d["n"] > 0 else 0
        lines.append(
            f"  {r:<15} {d['n']:>4}  WR:{wr:>5.1f}%  PnL:{d['pnl']:>+10.2f}"
        )

    # Monte Carlo 结果
    mc = data.get("monte_carlo")
    if mc:
        lines.append("--- Monte Carlo ---")
        lines.append(
            f"  Sims:{mc.get('num_simulations', '?')}"
            f"  Sharpe p={mc.get('sharpe_p_value', '?')}"
            f"  PF p={mc.get('profit_factor_p_value', '?')}"
            f"  Significant: {'YES' if mc.get('is_significant') else 'NO'}"
        )

    # Filter 统计
    fs = data.get("filter_stats")
    if fs:
        lines.append("--- Filters ---")
        total_eval = fs.get("total_evaluations", 0)
        total_block = fs.get("total_blocked", 0)
        block_rate = total_block / total_eval * 100 if total_eval > 0 else 0
        lines.append(
            f"  Evaluations:{total_eval}  Blocked:{total_block} ({block_rate:.1f}%)"
        )
        for reason, count in sorted(
            fs.get("blocked_reasons", {}).items(),
            key=lambda x: x[1],
            reverse=True,
        ):
            lines.append(f"    {reason}: {count}")

    return "\n".join(lines)


def _render_minimal(data: dict) -> str:
    m = data["metrics"]
    return (
        f"{data['tf']}: {m['trades']}trades WR:{m['win_rate']}% PnL:{m['pnl']} "
        f"PF:{m['pf']} Sharpe:{m['sharpe']} Sortino:{m['sortino']} "
        f"DD:{m['max_dd']}% Calmar:{m['calmar']} W/L:{m['win_loss_ratio']}"
    )


def _render_strategy_only(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{data['tf']} — {m['trades']} trades, WR:{m['win_rate']}%, PnL:{m['pnl']}, PF:{m['pf']}",
        f"  {'Strategy':<28} {'Trd':>4} {'Win':>4} {'WR%':>6} {'PnL':>10}",
    ]
    for name, s in sorted(
        data["strategy_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True
    ):
        wr = s["w"] / s["n"] * 100 if s["n"] > 0 else 0
        lines.append(
            f"  {name:<28} {s['n']:>4} {s['w']:>4} {wr:>5.1f}% {s['pnl']:>+10.2f}"
        )
    return "\n".join(lines)


def _render_regime_detail(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{data['tf']} — {m['trades']} trades, PnL:{m['pnl']}, PF:{m['pf']}",
    ]
    for regime, strategies in sorted(data["regime_strategy"].items()):
        r_data = data["regime_stats"].get(regime, {})
        r_wr = (
            r_data["w"] / r_data["n"] * 100 if r_data.get("n", 0) > 0 else 0
        )
        lines.append(
            f"  [{regime}] {r_data.get('n',0)} trades  WR:{r_wr:.1f}%"
            f"  PnL:{r_data.get('pnl',0):>+.2f}"
        )
        for sname, sd in sorted(
            strategies.items(), key=lambda x: x[1]["pnl"], reverse=True
        ):
            swr = sd["w"] / sd["n"] * 100 if sd["n"] > 0 else 0
            lines.append(
                f"    {sname:<26} {sd['n']:>3} {swr:>5.1f}% {sd['pnl']:>+10.2f}"
            )
    return "\n".join(lines)


def _render_exit_analysis(data: dict) -> str:
    m = data["metrics"]
    lines = [
        f"\n{data['tf']} — {m['trades']} trades, PnL:{m['pnl']}",
        f"  AvgWin:{m['avg_win']}  AvgLoss:{m['avg_loss']}  W/L Ratio:{m['win_loss_ratio']}",
        "--- Exit Reason Detail ---",
    ]
    total = sum(d["n"] for d in data["exit_stats"].values())
    for reason, d in sorted(
        data["exit_stats"].items(), key=lambda x: x[1]["n"], reverse=True
    ):
        pct = d["n"] / total * 100 if total > 0 else 0
        avg_bars = d["bars_held_sum"] / d["n"] if d["n"] > 0 else 0
        avg_pnl = d["pnl"] / d["n"] if d["n"] > 0 else 0
        lines.append(
            f"  {reason:<20} {d['n']:>4} ({pct:>5.1f}%)  "
            f"TotalPnL:{d['pnl']:>+10.2f}  AvgPnL:{avg_pnl:>+8.2f}  AvgBars:{avg_bars:.1f}"
        )
    return "\n".join(lines)


def _render_compare(results: List[dict]) -> str:
    """多 TF 对比表（--compare 模式）。"""
    if not results:
        return "No results."

    header = (
        f"  {'TF':<5} {'Trd':>5} {'WR%':>6} {'PnL':>10} {'PF':>7} "
        f"{'Sharpe':>7} {'Sortino':>8} {'Calmar':>7} {'DD%':>6} "
        f"{'AvgW':>8} {'AvgL':>8} {'W/L':>5} {'Exp':>8}"
    )
    sep = "-" * len(header)
    lines = [
        f"\n{'='*len(header)}",
        f" XAUUSD Multi-TF Comparison ({results[0]['start']} ~ {results[0]['end']})",
        f"{'='*len(header)}",
        header,
        sep,
    ]
    for data in results:
        m = data["metrics"]
        lines.append(
            f"  {data['tf']:<5} {m['trades']:>5} {m['win_rate']:>5.1f}% {m['pnl']:>+10.2f} {m['pf']:>7.3f} "
            f"{m['sharpe']:>7.3f} {m['sortino']:>8.3f} {m['calmar']:>7.3f} {m['max_dd']:>5.2f}% "
            f"{m['avg_win']:>8.2f} {m['avg_loss']:>8.2f} {m['win_loss_ratio']:>5.2f} {m['expectancy']:>+8.2f}"
        )
    lines.append(sep)

    # 最佳 TF 标注
    best_sharpe = max(results, key=lambda r: r["metrics"]["sharpe"])
    best_pnl = max(results, key=lambda r: r["metrics"]["pnl"])
    best_pf = max(results, key=lambda r: r["metrics"]["pf"])
    lines.append(
        f"  Best Sharpe: {best_sharpe['tf']} ({best_sharpe['metrics']['sharpe']})"
        f"  Best PnL: {best_pnl['tf']} ({best_pnl['metrics']['pnl']})"
        f"  Best PF: {best_pf['tf']} ({best_pf['metrics']['pf']})"
    )

    # 策略交叉表：哪些策略在哪些 TF 有交易
    all_strategies: set = set()
    for data in results:
        all_strategies.update(data["strategy_stats"].keys())
    if all_strategies:
        lines.append(f"\n--- Strategy × TF PnL ---")
        tf_names = [d["tf"] for d in results]
        lines.append(f"  {'Strategy':<28} " + " ".join(f"{tf:>8}" for tf in tf_names))
        for strat in sorted(all_strategies):
            vals = []
            for data in results:
                s = data["strategy_stats"].get(strat)
                if s:
                    vals.append(f"{s['pnl']:>+8.2f}")
                else:
                    vals.append(f"{'---':>8}")
            lines.append(f"  {strat:<28} " + " ".join(vals))

    return "\n".join(lines)


TEMPLATES = {
    "default": _render_default,
    "minimal": _render_minimal,
    "strategy_only": _render_strategy_only,
    "regime_detail": _render_regime_detail,
    "exit_analysis": _render_exit_analysis,
}


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="MT5Services backtest runner")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定回测使用哪个环境数据库",
    )
    parser.add_argument(
        "--tf", required=True, help="Timeframe(s), comma-separated: H1,M30,M15,M5"
    )
    parser.add_argument("--start", default="2025-12-30", help="Start date ISO format")
    parser.add_argument("--end", default="2026-03-30", help="End date ISO format")
    parser.add_argument(
        "--template",
        default="default",
        choices=list(TEMPLATES.keys()) + ["custom"],
    )
    parser.add_argument("--custom-template", default=None, help="Custom Python format string")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Output multi-TF comparison table",
    )
    parser.add_argument(
        "--monte-carlo",
        action="store_true",
        help="Force enable Monte Carlo (overrides backtest.ini)",
    )
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--no-filters", action="store_true")
    parser.add_argument("--sessions", default=None)
    parser.add_argument("--commission", type=float, default=None, help="Override commission_per_lot")
    parser.add_argument("--slippage", type=float, default=None, help="Override slippage_points")
    parser.add_argument("--show-config", action="store_true", help="Show config summary for each TF")
    parser.add_argument(
        "--strategies",
        default=None,
        help="Only run these registered strategies, comma-separated",
    )
    parser.add_argument(
        "--simulation-mode",
        default=None,
        choices=["research", "execution_feasibility"],
        help="Backtest execution semantics",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="Write structured result payload to JSON file",
    )
    parser.add_argument(
        "--no-auto-backfill",
        action="store_true",
        help="Disable automatic MT5 backfill when requested OHLC coverage is missing",
    )
    args = parser.parse_args()
    set_current_environment(args.environment)

    # 构建 CLI overrides（仅覆盖显式传入的参数）
    overrides: Dict[str, Any] = {}
    if args.min_confidence is not None:
        overrides["min_confidence"] = args.min_confidence
    if args.warmup is not None:
        overrides["warmup_bars"] = args.warmup
    if args.no_filters:
        overrides["filters_enabled"] = False
        overrides["filter_session_enabled"] = False
    if args.sessions is not None:
        overrides["filter_allowed_sessions"] = args.sessions
    if args.commission is not None:
        overrides["commission_per_lot"] = args.commission
    if args.slippage is not None:
        overrides["slippage_points"] = args.slippage
    if args.monte_carlo:
        overrides["monte_carlo_enabled"] = True
    if args.simulation_mode is not None:
        overrides["simulation_mode"] = args.simulation_mode

    timeframes = [t.strip().upper() for t in args.tf.split(",")]
    strategy_names = (
        [name.strip() for name in args.strategies.split(",") if name.strip()]
        if args.strategies
        else None
    )
    from src.ops.cli._coverage import ensure_ohlc_data_coverage

    coverage = ensure_ohlc_data_coverage(
        symbol="XAUUSD",
        timeframes=timeframes,
        start=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        auto_backfill=not args.no_auto_backfill,
    )
    render_fn = TEMPLATES.get(args.template, _render_default)

    all_results: List[dict] = []
    raw_results: List[dict] = []
    for tf in timeframes:
        sys.stderr.write(f"Running {tf}...\n")
        sys.stderr.flush()
        data = _run_single(
            tf,
            args.start,
            args.end,
            config_overrides=overrides,
            strategy_names=strategy_names,
        )
        raw_result = data.pop("_raw_result", None)
        if raw_result is not None:
            raw_results.append(raw_result.to_dict())
        all_results.append(data)

        if not args.compare:
            if args.template == "custom" and args.custom_template:
                print(args.custom_template.format(**data["metrics"], tf=data["tf"]))
            else:
                print(render_fn(data))

    # 多 TF 对比模式
    if args.compare and len(all_results) > 1:
        print(_render_compare(all_results))
    elif args.compare and len(all_results) == 1:
        print(render_fn(all_results[0]))

    if args.json_output:
        import json

        output_path = Path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": "XAUUSD",
            "coverage": {tf: info.to_dict() for tf, info in coverage.items()},
            "strategies": strategy_names or [],
            "results": all_results,
            "raw_results": raw_results,
        }
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    sys.stderr.write("Done.\n")


if __name__ == "__main__":
    main()

