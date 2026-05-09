"""StructuredPriceAction 重写版参数 grid 扫描器（plan §0zl 选项 1）。

PA 重写后参数集（无旧 BARRIER / 无 explore toggle）：

_why 硬条件：
  --adx-min          ADX 最低门槛（默认 18）
  --trend-bars-min   趋势 bar 数兜底门槛（默认 5）
  --bb-buy-max       buy 时 BB+KC 平均位置上限（默认 0.30）
  --bb-sell-min      sell 时 BB+KC 平均位置下限（默认 0.70）

_when 共振：
  --body-ratio-min        K 线 body_ratio 下限（默认 1.0）
  --close-pos-buy-max     buy 时 close_position 上限（默认 0.30）
  --close-pos-sell-min    sell 时 close_position 下限（默认 0.70）

出场（固定 Chandelier）：
  --aggression       Chandelier α 候选（默认 0.30/0.50/0.70）

用法：
    python -m src.ops.cli.pa_barrier_grid \
        --environment live --tf M15 \
        --start 2025-04-01 --end 2026-04-30 \
        --adx-min 15,18,22 --bb-buy-max 0.20,0.30 \
        --body-ratio-min 1.0,1.3 --aggression 0.30,0.50

PA 当前 deployment status=candidate，本 CLI 自动启用 research_mode 绕过
ADR-009 deployment gate（保留 audit_reason traceability）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
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
    adx_min: float,
    trend_bars_min: int,
    bb_buy_max: float,
    bb_sell_min: float,
    body_ratio_min: float,
    close_pos_buy_max: float,
    close_pos_sell_min: float,
    aggression: float,
) -> Dict[str, Any]:
    """Monkey-patch PA 类变量（重写版参数集，无旧 BARRIER/toggle）。"""
    import importlib

    module = importlib.import_module(_PA_CLASS_PATH[0])
    cls = getattr(module, _PA_CLASS_PATH[1])
    originals = {
        "_adx_min": cls._adx_min,
        "_trend_bars_min": cls._trend_bars_min,
        "_bb_buy_max": cls._bb_buy_max,
        "_bb_sell_min": cls._bb_sell_min,
        "_body_ratio_min": cls._body_ratio_min,
        "_close_pos_buy_max": cls._close_pos_buy_max,
        "_close_pos_sell_min": cls._close_pos_sell_min,
        "_aggression": cls._aggression,
    }
    cls._adx_min = adx_min
    cls._trend_bars_min = trend_bars_min
    cls._bb_buy_max = bb_buy_max
    cls._bb_sell_min = bb_sell_min
    cls._body_ratio_min = body_ratio_min
    cls._close_pos_buy_max = close_pos_buy_max
    cls._close_pos_sell_min = close_pos_sell_min
    cls._aggression = aggression
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
    adx_min: float,
    trend_bars_min: int,
    bb_buy_max: float,
    bb_sell_min: float,
    body_ratio_min: float,
    close_pos_buy_max: float,
    close_pos_sell_min: float,
    aggression: float,
) -> Dict[str, Any]:
    originals = _patch_pa(
        adx_min=adx_min,
        trend_bars_min=trend_bars_min,
        bb_buy_max=bb_buy_max,
        bb_sell_min=bb_sell_min,
        body_ratio_min=body_ratio_min,
        close_pos_buy_max=close_pos_buy_max,
        close_pos_sell_min=close_pos_sell_min,
        aggression=aggression,
    )
    try:
        from src.backtesting import component_factory as cf

        for attr in ("_cached_signal_config", "_cached_components"):
            if hasattr(cf, attr) and not callable(getattr(cf, attr)):
                delattr(cf, attr)

        from src.ops.cli.backtest_runner import _run_single

        audit = (
            f"pa_grid:adx={adx_min},tb={trend_bars_min},"
            f"bb_buy={bb_buy_max},bb_sell={bb_sell_min},"
            f"body={body_ratio_min},cp_buy={close_pos_buy_max},"
            f"cp_sell={close_pos_sell_min},α={aggression}"
        )
        data = _run_single(
            tf,
            start,
            end,
            research_mode_audit_reason=audit,
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
    """plan T2 验收门槛：PF≥1.0 + DD<30% + trades≥30。"""
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
        description="StructuredPriceAction 重写版参数 grid 扫描"
    )
    parser.add_argument("--environment", choices=["live", "demo"], required=True)
    parser.add_argument("--tf", required=True, help="单一时间框架，例如 M15 / M30")
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-04-30")
    parser.add_argument(
        "--adx-min",
        default="18",
        help="_why ADX 最低门槛候选（默认 18）",
    )
    parser.add_argument(
        "--trend-bars-min",
        default="5",
        help="_why trend_bars 兜底最小长度候选（默认 5）",
    )
    parser.add_argument(
        "--bb-buy-max",
        default="0.30",
        help="_why buy 时 BB+KC 平均位置上限候选（默认 0.30）",
    )
    parser.add_argument(
        "--bb-sell-min",
        default="0.70",
        help="_why sell 时 BB+KC 平均位置下限候选（默认 0.70）",
    )
    parser.add_argument(
        "--body-ratio-min",
        default="1.0",
        help="_when body_ratio 下限候选（默认 1.0）",
    )
    parser.add_argument(
        "--close-pos-buy-max",
        default="0.30",
        help="_when buy 时 close_position 上限候选（默认 0.30）",
    )
    parser.add_argument(
        "--close-pos-sell-min",
        default="0.70",
        help="_when sell 时 close_position 下限候选（默认 0.70）",
    )
    parser.add_argument(
        "--aggression",
        default="0.30,0.50,0.70",
        help="Chandelier α 候选（默认 0.30/0.50/0.70）",
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
        help="安全上限：组合数超此值则中止",
    )
    args = parser.parse_args()

    set_current_environment(args.environment)

    adx_min_values = _parse_floats(args.adx_min)
    trend_bars_values = _parse_ints(args.trend_bars_min)
    bb_buy_values = _parse_floats(args.bb_buy_max)
    bb_sell_values = _parse_floats(args.bb_sell_min)
    body_min_values = _parse_floats(args.body_ratio_min)
    cp_buy_values = _parse_floats(args.close_pos_buy_max)
    cp_sell_values = _parse_floats(args.close_pos_sell_min)
    aggression_values = _parse_floats(args.aggression)

    # 元组: (adx, tb, bb_buy, bb_sell, body, cp_buy, cp_sell, α)
    combinations: List[Tuple[float, int, float, float, float, float, float, float]] = (
        list(
            itertools.product(
                adx_min_values,
                trend_bars_values,
                bb_buy_values,
                bb_sell_values,
                body_min_values,
                cp_buy_values,
                cp_sell_values,
                aggression_values,
            )
        )
    )
    total = len(combinations)
    if total == 0:
        print("空参数空间。")
        sys.exit(1)
    if total > args.max_combinations:
        print(f"组合数 {total} 超出 --max-combinations={args.max_combinations}")
        sys.exit(1)

    print(f"\n{'=' * 116}")
    print(
        f"PA grid (重写版): tf={args.tf} {args.start}~{args.end} "
        f"({total} combinations, 固定 Chandelier 出场)"
    )
    print(
        f"adx={adx_min_values} | tb={trend_bars_values} | "
        f"bb_buy={bb_buy_values} | bb_sell={bb_sell_values} | "
        f"body={body_min_values} | cp_buy={cp_buy_values} | "
        f"cp_sell={cp_sell_values} | α={aggression_values}"
    )
    print(f"{'=' * 116}\n")

    header = (
        f"{'adx':>4} {'tb':>3} {'bbB':>5} {'bbS':>5} {'body':>5} "
        f"{'cpB':>5} {'cpS':>5} {'α':>5} "
        f"{'trades':>7} {'WR':>7} {'PnL':>10} {'PF':>6} "
        f"{'Sharpe':>8} {'MaxDD':>7} {'Gate':>22}"
    )
    print(header)
    print("-" * len(header))

    results: List[Dict[str, Any]] = []
    for adx, tb, bb_buy, bb_sell, body, cp_buy, cp_sell, alpha in combinations:
        try:
            data = _run_single_combo(
                tf=args.tf,
                start=args.start,
                end=args.end,
                adx_min=adx,
                trend_bars_min=tb,
                bb_buy_max=bb_buy,
                bb_sell_min=bb_sell,
                body_ratio_min=body,
                close_pos_buy_max=cp_buy,
                close_pos_sell_min=cp_sell,
                aggression=alpha,
            )
            summary = _summarize(data)
            summary.update(
                {
                    "adx_min": adx,
                    "trend_bars_min": tb,
                    "bb_buy_max": bb_buy,
                    "bb_sell_min": bb_sell,
                    "body_ratio_min": body,
                    "close_pos_buy_max": cp_buy,
                    "close_pos_sell_min": cp_sell,
                    "aggression": alpha,
                }
            )
            summary["gate"] = _evaluate_promotion_gate(summary)
            results.append(summary)
            print(
                f"{adx:>4.1f} {tb:>3d} {bb_buy:>5.2f} {bb_sell:>5.2f} "
                f"{body:>5.2f} {cp_buy:>5.2f} {cp_sell:>5.2f} "
                f"{alpha:>5.2f} "
                f"{summary['total_trades']:>7d} "
                f"{summary['win_rate_pct']:>6.1f}% "
                f"{summary['pnl']:>+10.2f} "
                f"{summary['profit_factor']:>6.2f} "
                f"{summary['sharpe']:>+8.3f} "
                f"{summary['max_dd_pct']:>6.2f}% "
                f"{summary['gate']:>22}"
            )
        except Exception as exc:
            print(
                f"{adx:>4.1f} {tb:>3d} {bb_buy:>5.2f} {bb_sell:>5.2f} "
                f"{body:>5.2f} {cp_buy:>5.2f} {cp_sell:>5.2f} "
                f"{alpha:>5.2f} FAILED: {exc}"
            )

    if not results:
        print("\n无任何组合产出结果。")
        sys.exit(1)

    promotable = [r for r in results if r["gate"] == "promotable"]
    print(f"\n{'=' * 88}")
    print(
        f"通过 promotion gate (PF≥1.0 + DD<30% + trades≥30) 的组合："
        f"{len(promotable)} / {total}"
    )
    print(f"{'=' * 88}\n")

    if promotable:
        print(f"Top {min(args.top, len(promotable))} promotable by PF:")
        promotable.sort(key=lambda r: r["profit_factor"], reverse=True)
        for r in promotable[: args.top]:
            print(
                f"  adx={r['adx_min']:.1f} tb={r['trend_bars_min']} "
                f"bb_buy={r['bb_buy_max']:.2f} bb_sell={r['bb_sell_min']:.2f} "
                f"body={r['body_ratio_min']:.2f} "
                f"cp_buy={r['close_pos_buy_max']:.2f} "
                f"cp_sell={r['close_pos_sell_min']:.2f} α={r['aggression']:.2f} | "
                f"PF={r['profit_factor']:.2f} WR={r['win_rate_pct']:.1f}% "
                f"trades={r['total_trades']} DD={r['max_dd_pct']:.2f}%"
            )
    else:
        results.sort(key=lambda r: r["profit_factor"], reverse=True)
        print(f"⚠ 无 promotable 组合。Top {args.top} by PF（未通过 gate）:")
        for r in results[: args.top]:
            print(
                f"  adx={r['adx_min']:.1f} tb={r['trend_bars_min']} "
                f"bb_buy={r['bb_buy_max']:.2f} bb_sell={r['bb_sell_min']:.2f} "
                f"body={r['body_ratio_min']:.2f} "
                f"cp_buy={r['close_pos_buy_max']:.2f} "
                f"cp_sell={r['close_pos_sell_min']:.2f} α={r['aggression']:.2f} | "
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
                "adx_min": adx_min_values,
                "trend_bars_min": trend_bars_values,
                "bb_buy_max": bb_buy_values,
                "bb_sell_min": bb_sell_values,
                "body_ratio_min": body_min_values,
                "close_pos_buy_max": cp_buy_values,
                "close_pos_sell_min": cp_sell_values,
                "aggression": aggression_values,
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
