"""StructuredPriceAction BARRIER 三参数 grid 扫描器（plan T2）。

PA 不走 Chandelier trail 而是 BARRIER 模式（fixed SL/TP/timeout）。本 CLI
通过 monkey-patch StructuredPriceAction 的类变量扫 (sl_atr × tp_atr × time_bars)
+ adx_floor 联合参数空间，让运营层在不改源码、不污染 signal.local.ini 的前提下
找到 PF≥1.0 + DD<30% 的最优组合（plan T2 验收门槛）。

用法：
    python -m src.ops.cli.pa_barrier_grid \\
        --environment live --tf M15 \\
        --start 2025-04-01 --end 2026-04-30 \\
        --sl 1.0,1.5,2.0 --tp 2.0,2.5,3.0,3.5 \\
        --time-bars 12,20,30 --adx-floor 8,12,16

设计：
    1. 通过 monkey-patch 类变量，每次只 mutate StructuredPriceAction 4 个属性
    2. backtest_runner._run_single 复用主链 BacktestEngine（不另起进程，避免污染缓存）
    3. 输出按 PF 排序 top N，写 JSON 报告（可选 --json-output）

注意：
    - 仅扫 PA 单策略（PA 是当前唯一活跃信号策略，2026-04-30 清场后）
    - tick_recovery_probe（恢复轨）走独立 admission，不在本 grid 范围
    - 完整组合数大时建议先小范围验证（避免单次跑 1000+ 回测）
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)


_PA_PARAM_ATTRS = ("sl_atr", "tp_atr", "time_bars", "adx_floor")
_PA_CLASS_PATH = (
    "src.signals.strategies.structured.price_action_m15",
    "StructuredPriceAction",
)


def _parse_floats(text: str) -> List[float]:
    return [float(value.strip()) for value in text.split(",") if value.strip()]


def _parse_ints(text: str) -> List[int]:
    return [int(value.strip()) for value in text.split(",") if value.strip()]


def _patch_pa(
    *,
    sl_atr: float,
    tp_atr: float,
    time_bars: int,
    adx_floor: float,
    exit_mode: str = "barrier",
    aggression: float = 0.50,
    when_min_score: float = 0.5,
    when_consensus: bool = False,
    require_structure: bool = False,
    bb_extreme_buy: float = 0.25,
    bb_extreme_sell: float = 0.75,
) -> Dict[str, Any]:
    """Monkey-patch PA 类变量，返回原始值字典（用于 finally 还原）。

    plan §0zl A/B 方向参数全集：
    - exit_mode/aggression: C 方向（出场模式）
    - when_min_score/when_consensus: A 方向（_when 收紧）
    - require_structure: B 方向（_why 硬条件，禁 trend_bars 兜底）
    - bb_extreme_buy/sell: B 方向（_where 严格阈值）
    """
    import importlib

    module = importlib.import_module(_PA_CLASS_PATH[0])
    cls = getattr(module, _PA_CLASS_PATH[1])
    originals = {
        "_sl_atr": cls._sl_atr,
        "_tp_atr": cls._tp_atr,
        "_time_bars": cls._time_bars,
        "_adx_floor": cls._adx_floor,
        "_exit_mode": cls._exit_mode,
        "_aggression": cls._aggression,
        "_when_min_score": cls._when_min_score,
        "_require_when_consensus": cls._require_when_consensus,
        "_require_structure": cls._require_structure,
        "_bb_extreme_buy": cls._bb_extreme_buy,
        "_bb_extreme_sell": cls._bb_extreme_sell,
    }
    cls._sl_atr = sl_atr
    cls._tp_atr = tp_atr
    cls._time_bars = time_bars
    cls._adx_floor = adx_floor
    cls._exit_mode = exit_mode
    cls._aggression = aggression
    cls._when_min_score = when_min_score
    cls._require_when_consensus = when_consensus
    cls._require_structure = require_structure
    cls._bb_extreme_buy = bb_extreme_buy
    cls._bb_extreme_sell = bb_extreme_sell
    return originals


def _restore_pa(originals: Dict[str, Any]) -> None:
    import importlib

    module = importlib.import_module(_PA_CLASS_PATH[0])
    cls = getattr(module, _PA_CLASS_PATH[1])
    for attr, value in originals.items():
        setattr(cls, attr, value)


def _run_single_combo(
    *,
    tf: str,
    start: str,
    end: str,
    sl_atr: float,
    tp_atr: float,
    time_bars: int,
    adx_floor: float,
    exit_mode: str = "barrier",
    aggression: float = 0.50,
    when_min_score: float = 0.5,
    when_consensus: bool = False,
    require_structure: bool = False,
    bb_extreme_buy: float = 0.25,
    bb_extreme_sell: float = 0.75,
) -> Dict[str, Any]:
    originals = _patch_pa(
        sl_atr=sl_atr,
        tp_atr=tp_atr,
        time_bars=time_bars,
        adx_floor=adx_floor,
        exit_mode=exit_mode,
        aggression=aggression,
        when_min_score=when_min_score,
        when_consensus=when_consensus,
        require_structure=require_structure,
        bb_extreme_buy=bb_extreme_buy,
        bb_extreme_sell=bb_extreme_sell,
    )
    try:
        # 清 component_factory 缓存确保策略实例重建
        from src.backtesting import component_factory as cf

        for attr in ("_cached_signal_config", "_cached_components"):
            if hasattr(cf, attr) and not callable(getattr(cf, attr)):
                delattr(cf, attr)

        from src.ops.cli.backtest_runner import _run_single

        # PA 当前 deployment status=candidate（PF 0.29 / DD 99% 待重设计），
        # 默认 backtest_runner 会被 deployment gate 排除。本 CLI 是研究路径，
        # 通过 research_disabled gate + audit_reason 显式绕过 ADR-009。
        audit_parts = (
            f"pa_grid_scan:exit={exit_mode},sl={sl_atr},tp={tp_atr}"
            f",tb={time_bars},adx={adx_floor}"
        )
        if exit_mode == "chandelier":
            audit_parts = f"pa_grid_scan:exit=chandelier,α={aggression},adx={adx_floor}"
        data = _run_single(
            tf,
            start,
            end,
            research_mode_audit_reason=audit_parts,
            strategy_names=["structured_price_action"],
        )
        return data
    finally:
        _restore_pa(originals)


def _summarize(data: Dict[str, Any]) -> Dict[str, Any]:
    metrics = data.get("metrics", {}) or {}
    return {
        "total_trades": int(metrics.get("trades", 0) or 0),
        "win_rate_pct": float(metrics.get("win_rate", 0.0) or 0.0),
        "pnl": float(metrics.get("pnl", 0.0) or 0.0),
        "profit_factor": float(metrics.get("pf", 0.0) or 0.0),
        "sharpe": float(metrics.get("sharpe", 0.0) or 0.0),
        "max_dd_pct": float(metrics.get("max_dd", 0.0) or 0.0),
        "avg_bars_held": float(metrics.get("avg_bars_held", 0.0) or 0.0),
    }


def _evaluate_promotion_gate(summary: Dict[str, Any]) -> str:
    """Plan T2 验收门槛：PF≥1.0 + DD<30% + trades≥30。"""
    if summary["total_trades"] < 30:
        return "insufficient_samples"
    if summary["profit_factor"] < 1.0:
        return "pf_below_threshold"
    if summary["max_dd_pct"] >= 30.0:
        return "dd_above_threshold"
    return "promotable"


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(
        description="StructuredPriceAction BARRIER 参数 grid 扫描"
    )
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--tf", required=True, help="单一时间框架，例如 M15 / M30")
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-04-30")
    parser.add_argument(
        "--sl",
        default="1.0,1.5,2.0",
        help="SL ATR 倍数候选（逗号分隔），默认 1.0/1.5/2.0",
    )
    parser.add_argument(
        "--tp",
        default="2.0,2.5,3.0,3.5",
        help="TP ATR 倍数候选，默认 2.0/2.5/3.0/3.5",
    )
    parser.add_argument(
        "--time-bars",
        default="12,20,30",
        help="time_bars 候选（int，逗号分隔），默认 12/20/30",
    )
    parser.add_argument(
        "--adx-floor",
        default="8,12,16",
        help="ADX floor 候选（float），默认 8/12/16",
    )
    parser.add_argument(
        "--exit-mode",
        choices=["barrier", "chandelier", "both"],
        default="barrier",
        help="出场模式：barrier (默认 SL/TP/Time) / chandelier (ATR trailing α) / both (各扫一遍)",
    )
    parser.add_argument(
        "--aggression",
        default="0.30,0.50,0.70",
        help="Chandelier α 候选（exit-mode=chandelier|both 时启用），默认 0.30/0.50/0.70",
    )
    parser.add_argument(
        "--when-min-score",
        default="0.5",
        help=(
            "_when 形态分数下限候选 (A 方向，plan §0zl)：0.5 (全部形态) / "
            "0.65 (去 big_bar) / 0.7 (去 big_bar+rejection) / 0.8 (仅 pin/engulfing/three)"
        ),
    )
    parser.add_argument(
        "--when-consensus",
        choices=["false", "true", "both"],
        default="false",
        help="是否要求 _when 多形态共振 (≥2 形态同时触发)。both 时各扫一遍",
    )
    parser.add_argument(
        "--require-structure",
        choices=["false", "true", "both"],
        default="false",
        help=(
            "B 方向：_why 是否要求 structure_type≠0（禁 trend_bars 兜底）。"
            "默认 false（保持当前 PA 行为）。both 时对比两种"
        ),
    )
    parser.add_argument(
        "--bb-extreme-buy",
        default="0.25",
        help="B 方向：_where buy 时 BB position 上限候选（默认 0.25，严格 0.15）",
    )
    parser.add_argument(
        "--bb-extreme-sell",
        default="0.75",
        help="B 方向：_where sell 时 BB position 下限候选（默认 0.75，严格 0.85）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="按 PF 排序展示前 N 个组合",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="JSON 报告输出路径（可选）",
    )
    parser.add_argument(
        "--max-combinations",
        type=int,
        default=200,
        help="安全上限：组合数超此值则中止（避免误跑 1000+ 回测）",
    )
    args = parser.parse_args()

    set_current_environment(args.environment)

    sl_values = _parse_floats(args.sl)
    tp_values = _parse_floats(args.tp)
    time_bars_values = _parse_ints(args.time_bars)
    adx_floor_values = _parse_floats(args.adx_floor)
    aggression_values = _parse_floats(args.aggression)
    when_min_score_values = _parse_floats(args.when_min_score)
    if args.when_consensus == "both":
        when_consensus_values = [False, True]
    elif args.when_consensus == "true":
        when_consensus_values = [True]
    else:
        when_consensus_values = [False]
    if args.require_structure == "both":
        require_structure_values = [False, True]
    elif args.require_structure == "true":
        require_structure_values = [True]
    else:
        require_structure_values = [False]
    bb_buy_values = _parse_floats(args.bb_extreme_buy)
    bb_sell_values = _parse_floats(args.bb_extreme_sell)

    # 构造组合：每个 mode × when × structure × bb_extreme 独立空间
    # 元组: (sl, tp, tb, adx, exit, agg, when_min, consensus, req_struct, bb_buy, bb_sell)
    combinations: List[
        Tuple[float, float, int, float, str, float, float, bool, bool, float, float]
    ] = []
    for when_min, when_cons, req_struct, bb_buy, bb_sell in itertools.product(
        when_min_score_values,
        when_consensus_values,
        require_structure_values,
        bb_buy_values,
        bb_sell_values,
    ):
        if args.exit_mode in ("barrier", "both"):
            for sl, tp, tb, adx in itertools.product(
                sl_values, tp_values, time_bars_values, adx_floor_values
            ):
                combinations.append(
                    (
                        sl,
                        tp,
                        tb,
                        adx,
                        "barrier",
                        0.50,
                        when_min,
                        when_cons,
                        req_struct,
                        bb_buy,
                        bb_sell,
                    )
                )
        if args.exit_mode in ("chandelier", "both"):
            for adx, alpha in itertools.product(adx_floor_values, aggression_values):
                combinations.append(
                    (
                        0.0,
                        0.0,
                        0,
                        adx,
                        "chandelier",
                        alpha,
                        when_min,
                        when_cons,
                        req_struct,
                        bb_buy,
                        bb_sell,
                    )
                )

    total = len(combinations)
    if total == 0:
        print("空参数空间，请检查 --sl/--tp/--time-bars/--adx-floor/--aggression。")
        sys.exit(1)
    if total > args.max_combinations:
        print(
            f"组合数 {total} 超出 --max-combinations={args.max_combinations}；"
            f"请缩小参数空间或显式提高 --max-combinations。"
        )
        sys.exit(1)

    print(f"\n{'='*116}")
    print(
        f"PA grid: tf={args.tf} {args.start}~{args.end} "
        f"({total} combinations, exit_mode={args.exit_mode})"
    )
    print(
        f"sl={sl_values} | tp={tp_values} | tb={time_bars_values} | "
        f"adx={adx_floor_values} | α={aggression_values} | "
        f"when_min={when_min_score_values} | cons={when_consensus_values} | "
        f"req_str={require_structure_values} | bb_buy={bb_buy_values} | "
        f"bb_sell={bb_sell_values}"
    )
    print(f"{'='*116}\n")

    header = (
        f"{'mode':>10} {'sl':>5} {'tp':>5} {'tb':>4} {'adx':>5} {'α':>5} "
        f"{'wmin':>5} {'cons':>5} {'rstr':>5} {'bbB':>5} {'bbS':>5} "
        f"{'trades':>7} {'WR':>7} {'PnL':>10} {'PF':>6} "
        f"{'Sharpe':>8} {'MaxDD':>7} {'Gate':>22}"
    )
    print(header)
    print("-" * len(header))

    results: List[Dict[str, Any]] = []
    for (
        sl_atr,
        tp_atr,
        time_bars,
        adx_floor,
        exit_mode,
        aggression,
        when_min,
        when_cons,
        req_struct,
        bb_buy,
        bb_sell,
    ) in combinations:
        try:
            sl_use = sl_atr if exit_mode == "barrier" else 1.5
            tp_use = tp_atr if exit_mode == "barrier" else 2.5
            tb_use = time_bars if exit_mode == "barrier" else 20
            data = _run_single_combo(
                tf=args.tf,
                start=args.start,
                end=args.end,
                sl_atr=sl_use,
                tp_atr=tp_use,
                time_bars=tb_use,
                adx_floor=adx_floor,
                exit_mode=exit_mode,
                aggression=aggression,
                when_min_score=when_min,
                when_consensus=when_cons,
                require_structure=req_struct,
                bb_extreme_buy=bb_buy,
                bb_extreme_sell=bb_sell,
            )
            summary = _summarize(data)
            summary.update(
                {
                    "exit_mode": exit_mode,
                    "sl_atr": sl_atr,
                    "tp_atr": tp_atr,
                    "time_bars": time_bars,
                    "adx_floor": adx_floor,
                    "aggression": aggression,
                    "when_min_score": when_min,
                    "when_consensus": when_cons,
                    "require_structure": req_struct,
                    "bb_extreme_buy": bb_buy,
                    "bb_extreme_sell": bb_sell,
                }
            )
            summary["gate"] = _evaluate_promotion_gate(summary)
            results.append(summary)
            cons_str = "Y" if when_cons else "N"
            rstr_str = "Y" if req_struct else "N"
            print(
                f"{exit_mode:>10} "
                f"{sl_atr:>5.2f} {tp_atr:>5.2f} {time_bars:>4d} "
                f"{adx_floor:>5.1f} {aggression:>5.2f} "
                f"{when_min:>5.2f} {cons_str:>5} {rstr_str:>5} "
                f"{bb_buy:>5.2f} {bb_sell:>5.2f} "
                f"{summary['total_trades']:>7d} "
                f"{summary['win_rate_pct']:>6.1f}% "
                f"{summary['pnl']:>+10.2f} "
                f"{summary['profit_factor']:>6.2f} "
                f"{summary['sharpe']:>+8.3f} "
                f"{summary['max_dd_pct']:>6.2f}% "
                f"{summary['gate']:>22}"
            )
        except Exception as exc:
            cons_str = "Y" if when_cons else "N"
            rstr_str = "Y" if req_struct else "N"
            print(
                f"{exit_mode:>10} {sl_atr:>5.2f} {tp_atr:>5.2f} "
                f"{time_bars:>4d} {adx_floor:>5.1f} {aggression:>5.2f} "
                f"{when_min:>5.2f} {cons_str:>5} {rstr_str:>5} "
                f"{bb_buy:>5.2f} {bb_sell:>5.2f} FAILED: {exc}"
            )

    if not results:
        print("\n无任何组合产出结果。")
        sys.exit(1)

    promotable = [r for r in results if r["gate"] == "promotable"]
    print(f"\n{'='*88}")
    print(
        f"通过 promotion gate (PF≥1.0 + DD<30% + trades≥30) 的组合："
        f"{len(promotable)} / {total}"
    )
    print(f"{'='*88}\n")

    if promotable:
        print(f"Top {min(args.top, len(promotable))} promotable by PF:")
        promotable.sort(key=lambda r: r["profit_factor"], reverse=True)
        for r in promotable[: args.top]:
            print(
                f"  sl={r['sl_atr']:.2f} tp={r['tp_atr']:.2f} "
                f"tb={r['time_bars']} adx_floor={r['adx_floor']:.1f} | "
                f"PF={r['profit_factor']:.2f} WR={r['win_rate_pct']:.1f}% "
                f"trades={r['total_trades']} DD={r['max_dd_pct']:.2f}%"
            )
    else:
        # 兜底：显示 PF 最高的（即使未达 gate）
        results.sort(key=lambda r: r["profit_factor"], reverse=True)
        print(f"⚠ 无 promotable 组合。Top {args.top} by PF（未通过 gate）:")
        for r in results[: args.top]:
            print(
                f"  sl={r['sl_atr']:.2f} tp={r['tp_atr']:.2f} "
                f"tb={r['time_bars']} adx_floor={r['adx_floor']:.1f} | "
                f"PF={r['profit_factor']:.2f} WR={r['win_rate_pct']:.1f}% "
                f"trades={r['total_trades']} DD={r['max_dd_pct']:.2f}% "
                f"({r['gate']})"
            )

    if args.json_output:
        report = {
            "tf": args.tf,
            "start": args.start,
            "end": args.end,
            "param_space": {
                "sl_atr": sl_values,
                "tp_atr": tp_values,
                "time_bars": time_bars_values,
                "adx_floor": adx_floor_values,
            },
            "total_combinations": total,
            "promotable_count": len(promotable),
            "results": results,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_output).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nJSON 报告已写入：{args.json_output}")


if __name__ == "__main__":
    main()
