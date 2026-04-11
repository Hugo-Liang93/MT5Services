"""SL/TP ATR 倍数网格搜索 — 找到最优止损止盈距离组合。

用法：
    python -m src.ops.cli.sltp_grid_search --tf H1
    python -m src.ops.cli.sltp_grid_search --tf H1 --sl-range 1.5,2.0,2.5,3.0,3.5 --tp-range 2.0,3.0,4.0,5.0
    python -m src.ops.cli.sltp_grid_search --tf M30 --top 10

原理：
    临时覆盖 sizing.py 的 TIMEFRAME_SL_TP 表，对每个 (SL, TP) 组合跑一次回测。
    输出按 Sharpe 排序的结果矩阵。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import warnings
warnings.filterwarnings("ignore")
import logging
logging.disable(logging.CRITICAL)

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _run_with_sltp(
    tf: str,
    start: str,
    end: str,
    sl_mult: float,
    tp_mult: float,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> dict:
    """运行单次回测，临时覆盖 SL/TP ATR 倍数。"""
    import src.trading.execution.sizing as sizing_mod

    # 临时覆盖 TIMEFRAME_SL_TP
    original = sizing_mod.TIMEFRAME_SL_TP.get(tf, {}).copy()
    sizing_mod.TIMEFRAME_SL_TP[tf] = {
        "sl_atr_mult": sl_mult,
        "tp_atr_mult": tp_mult,
    }

    try:
        from tools.backtest_runner import _run_single
        overrides = dict(config_overrides or {})
        # 禁用 Monte Carlo（加速搜索）
        overrides["monte_carlo_enabled"] = False
        data = _run_single(tf, start, end, config_overrides=overrides)
    finally:
        # 恢复原值
        if original:
            sizing_mod.TIMEFRAME_SL_TP[tf] = original
        else:
            sizing_mod.TIMEFRAME_SL_TP.pop(tf, None)

    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="SL/TP ATR multiplier grid search")
    parser.add_argument("--tf", required=True, help="Timeframe")
    parser.add_argument("--start", default="2025-12-30")
    parser.add_argument("--end", default="2026-03-30")
    parser.add_argument(
        "--sl-range",
        default="1.5,2.0,2.5,3.0,3.5,4.0",
        help="SL multipliers (comma-separated)",
    )
    parser.add_argument(
        "--tp-range",
        default="1.5,2.0,2.5,3.0,4.0,5.0",
        help="TP multipliers (comma-separated)",
    )
    parser.add_argument("--top", type=int, default=15, help="Show top N results")
    args = parser.parse_args()

    tf = args.tf.strip().upper()
    sl_values = [float(x.strip()) for x in args.sl_range.split(",")]
    tp_values = [float(x.strip()) for x in args.tp_range.split(",")]

    total = len(sl_values) * len(tp_values)
    sys.stderr.write(
        f"Grid search: {tf} XAUUSD, {len(sl_values)} SL × {len(tp_values)} TP = {total} combinations\n"
    )

    results: List[dict] = []
    idx = 0

    for sl in sl_values:
        for tp in tp_values:
            # 跳过 SL >= TP 的不合理组合
            if sl >= tp:
                idx += 1
                continue

            idx += 1
            sys.stderr.write(f"  [{idx}/{total}] SL={sl} TP={tp}...\n")
            sys.stderr.flush()

            try:
                data = _run_with_sltp(tf, args.start, args.end, sl, tp)
                m = data["metrics"]
                results.append({
                    "sl": sl,
                    "tp": tp,
                    "ratio": round(tp / sl, 2),
                    "trades": m["trades"],
                    "wr": m["win_rate"],
                    "pnl": m["pnl"],
                    "pf": m["pf"],
                    "sharpe": m["sharpe"],
                    "max_dd": m["max_dd"],
                    "avg_win": m["avg_win"],
                    "avg_loss": m["avg_loss"],
                    "wl": m["win_loss_ratio"],
                    "exp": m["expectancy"],
                })
            except Exception as e:
                sys.stderr.write(f"    FAILED: {e}\n")

    if not results:
        print("No valid results.")
        return

    # 按 Sharpe 排序
    results.sort(key=lambda r: r["sharpe"], reverse=True)

    # 输出
    print(f"\n{'='*100}")
    print(f" {tf} XAUUSD SL/TP Grid Search ({args.start} ~ {args.end})")
    print(f" Balance: from backtest.ini | {total} combinations tested")
    print(f"{'='*100}")
    print(
        f"  {'#':>3} {'SL':>5} {'TP':>5} {'R':>5} {'Trd':>5} {'WR%':>6} "
        f"{'PnL':>10} {'PF':>7} {'Sharpe':>7} {'DD%':>6} "
        f"{'AvgW':>8} {'AvgL':>8} {'W/L':>5} {'Exp':>8}"
    )
    print("-" * 100)

    for i, r in enumerate(results[: args.top]):
        marker = " ***" if r["sharpe"] > 0 and r["pf"] > 1.0 else ""
        print(
            f"  {i+1:>3} {r['sl']:>5.1f} {r['tp']:>5.1f} {r['ratio']:>5.2f} "
            f"{r['trades']:>5} {r['wr']:>5.1f}% {r['pnl']:>+10.2f} {r['pf']:>7.3f} "
            f"{r['sharpe']:>7.3f} {r['max_dd']:>5.2f}% "
            f"{r['avg_win']:>8.2f} {r['avg_loss']:>8.2f} {r['wl']:>5.2f} {r['exp']:>+8.2f}{marker}"
        )

    # 热力图（文本版）
    print(f"\n--- Sharpe Heatmap (SL rows × TP cols) ---")
    sharpe_map: Dict[tuple, float] = {(r["sl"], r["tp"]): r["sharpe"] for r in results}
    print(f"  {'SL↓/TP→':>8}", end="")
    for tp in tp_values:
        print(f" {tp:>7.1f}", end="")
    print()
    for sl in sl_values:
        print(f"  {sl:>8.1f}", end="")
        for tp in tp_values:
            val = sharpe_map.get((sl, tp))
            if val is None:
                print(f" {'---':>7}", end="")
            elif val > 0:
                print(f" {val:>+7.3f}", end="")
            else:
                print(f" {val:>7.3f}", end="")
        print()

    sys.stderr.write("Done.\n")


if __name__ == "__main__":
    main()

