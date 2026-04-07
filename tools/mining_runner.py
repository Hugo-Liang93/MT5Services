"""信号挖掘运行器 — 本地直接执行数据挖掘并输出结构化摘要。

用法：
    python tools/mining_runner.py --tf H1 --start 2025-12-30 --end 2026-03-30
    python tools/mining_runner.py --tf H1 --analysis predictive_power
    python tools/mining_runner.py --tf H1 --analysis threshold --indicator rsi14
    python tools/mining_runner.py --tf H1,M30 --compare
    python tools/mining_runner.py --tf H1 --template minimal

分析类型（纯数据驱动，不涉及现有策略）：
    predictive_power  — 指标预测力分析（IC / 命中率 / 显著性）
    threshold         — 阈值扫描优化（最优买卖阈值 + CV 验证）
    rule_mining       — 多条件规则挖掘（决策树提取 IF-THEN 规则）

所有日志静默，仅输出摘要到 stdout。供 Claude Code 通过单次 Bash 调用获取结果。
"""

from __future__ import annotations

import argparse
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

warnings.filterwarnings("ignore")

import logging

logging.disable(logging.CRITICAL)

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _run_single(
    tf: str,
    start: str,
    end: str,
    *,
    analyses: Optional[List[str]] = None,
    indicator_filter: Optional[List[str]] = None,
) -> dict:
    """执行单个 TF 信号挖掘，返回结构化结果 dict。"""
    from src.backtesting.component_factory import build_backtest_components
    from src.research.config import load_research_config
    from src.research.runner import MiningRunner

    config = load_research_config()
    components = build_backtest_components()

    runner = MiningRunner(config=config, components=components)

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    result = runner.run(
        symbol="XAUUSD",
        timeframe=tf,
        start_time=start_dt,
        end_time=end_dt,
        analyses=analyses,
        indicator_filter=indicator_filter,
    )

    # 构建结构化输出
    output: Dict[str, Any] = {
        "tf": tf,
        "start": start,
        "end": end,
        "run_id": result.run_id,
    }

    if result.data_summary:
        ds = result.data_summary
        output["data_summary"] = {
            "n_bars": ds.n_bars,
            "train_bars": ds.train_bars,
            "test_bars": ds.test_bars,
            "regime_distribution": ds.regime_distribution,
            "n_indicator_fields": len(ds.available_indicators),
        }

    if result.predictive_power:
        sig = [r for r in result.predictive_power if r.is_significant]
        output["predictive_power"] = {
            "total_tested": len(result.predictive_power),
            "significant": len(sig),
            "top_10": [r.to_dict() for r in sig[:10]],
            # 统计增强：IR 稳定的发现
            "stable_ir": [
                r.to_dict()
                for r in sig
                if r.rolling_ic is not None and r.rolling_ic.information_ratio >= 0.5
            ][:5],
        }

    if result.threshold_sweeps:
        output["threshold_sweeps"] = [r.to_dict() for r in result.threshold_sweeps]

    if result.mined_rules:
        output["mined_rules"] = [r.to_dict() for r in result.mined_rules]

    if result.top_findings:
        output["top_findings"] = [f.to_dict() for f in result.top_findings[:15]]

    # 保留原始 MiningResult 供跨 TF 分析（不序列化）
    output["_raw_result"] = result

    return output


# ── 渲染模板 ────────────────────────────────────────────────────


def _render_default(data: dict) -> str:
    """完整摘要。"""
    lines: List[str] = []
    tf = data["tf"]
    ds = data.get("data_summary", {})

    lines.append(f"\n{'='*70}")
    lines.append(f" XAUUSD/{tf} Signal Mining ({data['start']} ~ {data['end']})")
    lines.append(f"{'='*70}")
    lines.append(
        f"  Bars: {ds.get('n_bars', '?')} "
        f"(train={ds.get('train_bars', '?')}, test={ds.get('test_bars', '?')})  "
        f"Fields: {ds.get('n_indicator_fields', '?')}"
    )

    regime = ds.get("regime_distribution", {})
    if regime:
        parts = [f"{k}={v}" for k, v in sorted(regime.items())]
        lines.append(f"  Regime: {', '.join(parts)}")

    # Predictive Power
    pp = data.get("predictive_power")
    if pp:
        lines.append(
            f"\n--- Predictive Power ({pp['significant']}/{pp['total_tested']} significant) ---"
        )
        for r in pp.get("top_10", []):
            sig_mark = "***" if r.get("is_significant") else ""
            # Rolling IC + IR
            ir_str = ""
            ric = r.get("rolling_ic")
            if ric:
                ir_val = ric.get("information_ratio", 0)
                ir_str = f"  IR={ir_val:+.2f}"
                if ir_val >= 0.5:
                    ir_str += " STABLE"
            # 排列检验
            perm_str = ""
            perm_p = r.get("permutation_p_value")
            if perm_p is not None:
                perm_str = f"  perm_p={perm_p:.3f}"
            lines.append(
                f"  {r['indicator']:<20} IC={r['ic']:+.3f}  "
                f"hit_above={r['hit_rate_above_median']*100:>5.1f}%  "
                f"hit_below={r['hit_rate_below_median']*100:>5.1f}%  "
                f"p={r['p_value']:.3f}  n={r['n_samples']}  "
                f"{r.get('forward_bars', '?')}-bar"
                f"{' (' + r['regime'] + ')' if r.get('regime') else ''}"
                f"{ir_str}{perm_str}  {sig_mark}"
            )

        # 稳定 IR 摘要
        stable = pp.get("stable_ir", [])
        if stable:
            lines.append(f"\n  IR >= 0.5 (time-stable signals): {len(stable)}")
            for r in stable:
                ric = r.get("rolling_ic", {})
                lines.append(
                    f"    {r['indicator']:<20} IR={ric.get('information_ratio', 0):+.2f}  "
                    f"mean_IC={ric.get('mean_ic', 0):+.3f}  "
                    f"n_windows={ric.get('n_windows', 0)}"
                )

    # Threshold Sweep
    ts_list = data.get("threshold_sweeps", [])
    if ts_list:
        lines.append(f"\n--- Threshold Sweep ({len(ts_list)} indicators) ---")
        for ts in ts_list:
            buy = ts.get("buy", {})
            sell = ts.get("sell", {})
            regime_str = f" ({ts['regime']})" if ts.get("regime") else ""
            lines.append(f"  {ts['indicator']} {ts['forward_bars']}-bar{regime_str}:")
            if buy.get("optimal_threshold") is not None:
                test_str = (
                    f" test={buy['test_hit_rate']*100:.1f}%"
                    if buy.get("test_hit_rate") is not None
                    else ""
                )
                fragile = " FRAGILE" if buy["cv_consistency"] < 0.6 else ""
                sig_str = " SIG" if buy.get("is_significant") else ""
                perm_str = (
                    f" perm_p={buy['permutation_p']:.3f}"
                    if buy.get("permutation_p") is not None
                    else ""
                )
                lines.append(
                    f"    BUY  <= {buy['optimal_threshold']:>8.3f}  "
                    f"hit={buy['hit_rate']*100:>5.1f}%{test_str}  "
                    f"n={buy['n_signals']}  CV={buy['cv_consistency']:.0%}"
                    f"{perm_str}{fragile}{sig_str}"
                )
            if sell.get("optimal_threshold") is not None:
                test_str = (
                    f" test={sell['test_hit_rate']*100:.1f}%"
                    if sell.get("test_hit_rate") is not None
                    else ""
                )
                fragile = " FRAGILE" if sell["cv_consistency"] < 0.6 else ""
                sig_str = " SIG" if sell.get("is_significant") else ""
                perm_str = (
                    f" perm_p={sell['permutation_p']:.3f}"
                    if sell.get("permutation_p") is not None
                    else ""
                )
                lines.append(
                    f"    SELL >= {sell['optimal_threshold']:>8.3f}  "
                    f"hit={sell['hit_rate']*100:>5.1f}%{test_str}  "
                    f"n={sell['n_signals']}  CV={sell['cv_consistency']:.0%}"
                    f"{perm_str}{fragile}{sig_str}"
                )

    # Mined Rules
    rules = data.get("mined_rules", [])
    if rules:
        lines.append(f"\n--- Mined Rules ({len(rules)} rules) ---")
        for i, r in enumerate(rules, 1):
            train = r.get("train", {})
            test = r.get("test", {})
            test_str = (
                f"  test={test['hit_rate']*100:.1f}%/n={test['n_samples']}"
                if test.get("hit_rate") is not None
                else ""
            )
            regime_str = f" [{r['regime']}]" if r.get("regime") else ""
            lines.append(f"\n  Rule #{i}: {r['direction'].upper()}{regime_str}")
            # Why/When/Where 结构
            s = r.get("structured", {})
            for role, label in [("why", "Why"), ("when", "When"), ("where", "Where")]:
                conds = s.get(role, [])
                if conds:
                    gate = "(hard)" if role != "where" else "(soft)"
                    cond_strs = [
                        f"{c['indicator']}.{c['field']} {c['operator']} {c['threshold']:.2f}"
                        for c in conds
                    ]
                    lines.append(f"    {label:>5} {gate}: {' AND '.join(cond_strs)}")
            lines.append(
                f"    train={train['hit_rate']*100:.1f}%/n={train['n_samples']}"
                f"  ret={train['mean_return']*100:+.3f}%"
                f"{test_str}"
            )

    # Top Findings
    findings = data.get("top_findings", [])
    if findings:
        lines.append(f"\n--- Top Findings ---")
        for f in findings[:10]:
            conf = {"high": "***", "medium": "**", "low": "*"}.get(f["confidence"], "")
            lines.append(f"  #{f['rank']:<3} [{f['category']}] {f['summary']} {conf}")
            lines.append(f"       → {f['action']}")

    lines.append("")
    return "\n".join(lines)


def _render_minimal(data: dict) -> str:
    """单行摘要。"""
    ds = data.get("data_summary", {})
    pp = data.get("predictive_power", {})
    ts_count = len(data.get("threshold_sweeps", []))
    findings = len(data.get("top_findings", []))

    return (
        f"{data['tf']}: {ds.get('n_bars', 0)} bars | "
        f"PP={pp.get('significant', 0)}/{pp.get('total_tested', 0)} sig | "
        f"TS={ts_count} indicators | "
        f"Findings={findings}"
    )


def _render_findings_only(data: dict) -> str:
    """仅 Top Findings。"""
    findings = data.get("top_findings", [])
    if not findings:
        return f"{data['tf']}: No significant findings."

    lines = [f"\n--- {data['tf']} Top Findings ---"]
    for f in findings:
        conf = {"high": "***", "medium": "**", "low": "*"}.get(f["confidence"], "")
        lines.append(f"  #{f['rank']:<3} [{f['category']}] {f['summary']} {conf}")
        lines.append(f"       → {f['action']}")
    return "\n".join(lines)


def _render_compare(results: List[dict]) -> str:
    """多 TF 对比。"""
    if not results:
        return "No results."

    lines = [
        f"\n{'='*70}",
        f" XAUUSD Multi-TF Mining Comparison ({results[0]['start']} ~ {results[0]['end']})",
        f"{'='*70}",
    ]

    header = f"  {'TF':<5} {'Bars':>6} {'PP_sig':>7} {'TS':>4} {'Finds':>6}"
    lines.append(header)
    lines.append("-" * len(header))

    for data in results:
        ds = data.get("data_summary", {})
        pp = data.get("predictive_power", {})
        lines.append(
            f"  {data['tf']:<5} {ds.get('n_bars', 0):>6} "
            f"{pp.get('significant', 0):>3}/{pp.get('total_tested', 0):<3} "
            f"{len(data.get('threshold_sweeps', [])):>4} "
            f"{len(data.get('top_findings', [])):>6}"
        )

    # 合并 findings
    all_findings = []
    for data in results:
        for f in data.get("top_findings", [])[:5]:
            f_copy = dict(f)
            f_copy["tf"] = data["tf"]
            all_findings.append(f_copy)

    if all_findings:
        all_findings.sort(key=lambda f: f["significance_score"], reverse=True)
        lines.append(f"\n--- Combined Top Findings ---")
        for i, f in enumerate(all_findings[:15], 1):
            conf = {"high": "***", "medium": "**", "low": "*"}.get(f["confidence"], "")
            lines.append(
                f"  #{i:<3} [{f['tf']}] [{f['category']}] {f['summary']} {conf}"
            )

    return "\n".join(lines)


def _render_cross_tf(raw_results: Dict[str, Any]) -> str:
    """跨 TF 一致性分析。"""
    from src.research.cross_tf_analyzer import analyze_cross_tf

    analysis = analyze_cross_tf(raw_results)
    lines: List[str] = []

    lines.append(f"\n{'='*70}")
    lines.append(f" Cross-TF Consistency Analysis ({', '.join(analysis.timeframes)})")
    lines.append(f"{'='*70}")
    lines.append(
        f"  Robust: {len(analysis.robust_signals)} | "
        f"Divergent: {len(analysis.divergent_signals)} | "
        f"TF-specific: {len(analysis.tf_specific_signals)}"
    )

    if analysis.robust_signals:
        lines.append(f"\n--- Robust Signals (direction consistent across TFs) ---")
        for sig in analysis.robust_signals[:15]:
            regime_str = f"[{sig.regime}]" if sig.regime else ""
            tf_detail = "  ".join(f"{s.timeframe}={s.ic:+.2f}" for s in sig.tf_scores)
            ir_str = f" avgIR={sig.avg_ir:.2f}" if sig.avg_ir else ""
            lines.append(
                f"  {sig.indicator}{regime_str:<30} "
                f"rec={sig.recommended_tf}  avg|IC|={sig.avg_abs_ic:.3f}{ir_str}"
            )
            lines.append(f"    {tf_detail}")

    if analysis.divergent_signals:
        lines.append(f"\n--- Divergent Signals (direction FLIPS across TFs) ---")
        for sig in analysis.divergent_signals[:10]:
            regime_str = f"[{sig.regime}]" if sig.regime else ""
            tf_detail = "  ".join(f"{s.timeframe}={s.ic:+.2f}" for s in sig.tf_scores)
            lines.append(f"  {sig.indicator}{regime_str:<30}  {tf_detail}")

    if analysis.tf_recommendations:
        lines.append(f"\n--- TF Recommendations ---")
        for tf, indicators in sorted(analysis.tf_recommendations.items()):
            if indicators:
                lines.append(f"  {tf}: {', '.join(indicators[:5])}")

    lines.append("")
    return "\n".join(lines)


TEMPLATES = {
    "default": _render_default,
    "minimal": _render_minimal,
    "findings_only": _render_findings_only,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5Services signal mining runner")
    parser.add_argument(
        "--tf", required=True, help="Timeframe(s), comma-separated: H1,M30,M15"
    )
    parser.add_argument("--start", default="2025-12-30", help="Start date ISO format")
    parser.add_argument("--end", default="2026-03-30", help="End date ISO format")
    parser.add_argument(
        "--analysis",
        default=None,
        help="Analysis type(s), comma-separated: predictive_power,threshold,rule_mining",
    )
    parser.add_argument(
        "--indicator",
        default=None,
        help="Indicator(s) to analyze (for threshold sweep), comma-separated",
    )
    parser.add_argument(
        "--template",
        default="default",
        choices=list(TEMPLATES.keys()),
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Output multi-TF comparison table",
    )
    args = parser.parse_args()

    tfs = [t.strip() for t in args.tf.split(",") if t.strip()]
    analyses = (
        [a.strip() for a in args.analysis.split(",") if a.strip()]
        if args.analysis
        else None
    )
    indicator_filter = (
        [i.strip() for i in args.indicator.split(",") if i.strip()]
        if args.indicator
        else None
    )

    results = []
    raw_results = {}  # tf → MiningResult（用于跨 TF 分析）
    for tf in tfs:
        data = _run_single(
            tf,
            args.start,
            args.end,
            analyses=analyses,
            indicator_filter=indicator_filter,
        )
        results.append(data)
        # 保存原始 MiningResult 用于跨 TF 分析
        if "_raw_result" in data:
            raw_results[tf] = data.pop("_raw_result")

    if args.compare and len(results) > 1:
        print(_render_compare(results))
        # 跨 TF 一致性分析
        if raw_results:
            print(_render_cross_tf(raw_results))
    else:
        render_fn = TEMPLATES[args.template]
        for data in results:
            print(render_fn(data))


if __name__ == "__main__":
    main()
