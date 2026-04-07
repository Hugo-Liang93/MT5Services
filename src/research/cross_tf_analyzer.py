"""跨 TF 一致性分析器 — 多 TF 挖掘后评估信号的跨周期稳健性。

准则：
  1. 在 2+ 个 TF 显著 + 方向一致 = 稳健信号 → 推荐 IC 最强的 TF
  2. 在 2+ 个 TF 显著但方向翻转 = 不同 TF 含义不同 → 各 TF 独立评估
  3. 仅 1 个 TF 显著 = TF 特异信号 → 需谨慎，可能是统计噪音
  4. 最佳 TF 推荐 = 综合 IC 强度 × IR 稳定性 × 样本量 × 排列检验

使用方式：
  results = {tf: MiningResult for tf in timeframes}
  analysis = analyze_cross_tf(results)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import IndicatorPredictiveResult, MiningResult


@dataclass(frozen=True)
class TFScore:
    """单个 TF 上的信号评分。"""

    timeframe: str
    ic: float
    ir: Optional[float]
    permutation_p: Optional[float]
    n_samples: int
    forward_bars: int


@dataclass(frozen=True)
class CrossTFSignal:
    """跨 TF 一致性分析结果。"""

    indicator: str  # e.g. "derived.momentum_consensus"
    regime: Optional[str]
    tf_scores: List[TFScore]
    n_tfs: int
    avg_abs_ic: float
    avg_ir: float
    direction_consistent: bool  # 所有 TF 的 IC 符号一致
    recommended_tf: str  # 推荐的最佳 TF
    robustness: str  # "robust" | "tf_divergent" | "tf_specific"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indicator": self.indicator,
            "regime": self.regime,
            "n_tfs": self.n_tfs,
            "avg_abs_ic": round(self.avg_abs_ic, 4),
            "avg_ir": round(self.avg_ir, 2),
            "direction_consistent": self.direction_consistent,
            "recommended_tf": self.recommended_tf,
            "robustness": self.robustness,
            "tf_scores": [
                {
                    "tf": s.timeframe,
                    "ic": round(s.ic, 4),
                    "ir": round(s.ir, 2) if s.ir is not None else None,
                    "perm_p": (
                        round(s.permutation_p, 3)
                        if s.permutation_p is not None
                        else None
                    ),
                    "n": s.n_samples,
                    "horizon": s.forward_bars,
                }
                for s in self.tf_scores
            ],
        }


@dataclass
class CrossTFAnalysis:
    """多 TF 挖掘的完整分析结果。"""

    timeframes: List[str]
    robust_signals: List[CrossTFSignal]  # 2+ TF 方向一致
    divergent_signals: List[CrossTFSignal]  # 2+ TF 方向翻转
    tf_specific_signals: List[CrossTFSignal]  # 仅 1 TF
    tf_recommendations: Dict[str, List[str]]  # {tf: [适合的 indicator 列表]}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeframes": self.timeframes,
            "robust_signals": [s.to_dict() for s in self.robust_signals],
            "divergent_signals": [s.to_dict() for s in self.divergent_signals],
            "tf_specific_signals": [s.to_dict() for s in self.tf_specific_signals],
            "tf_recommendations": self.tf_recommendations,
            "summary": {
                "n_robust": len(self.robust_signals),
                "n_divergent": len(self.divergent_signals),
                "n_tf_specific": len(self.tf_specific_signals),
            },
        }


def analyze_cross_tf(
    results: Dict[str, MiningResult],
    *,
    min_tfs_for_robust: int = 2,
) -> CrossTFAnalysis:
    """对多 TF 挖掘结果做跨周期一致性分析。

    Args:
        results: {timeframe: MiningResult}
        min_tfs_for_robust: 至少在 N 个 TF 显著才算跨 TF 信号

    Returns:
        CrossTFAnalysis
    """
    timeframes = sorted(results.keys())

    # 收集每个 (indicator.field, regime) 在各 TF 的最强结果
    signal_map: Dict[str, Dict[str, _BestEntry]] = {}  # key → {tf → entry}

    for tf, result in results.items():
        for r in result.predictive_power:
            if not r.is_significant:
                continue
            key = f"{r.indicator_name}.{r.field_name}"
            regime_key = f"{key}[{r.regime}]" if r.regime else key

            if regime_key not in signal_map:
                signal_map[regime_key] = {}
            existing = signal_map[regime_key].get(tf)
            if existing is None or abs(r.information_coefficient) > abs(existing.ic):
                signal_map[regime_key][tf] = _BestEntry(
                    ic=r.information_coefficient,
                    ir=(
                        r.rolling_ic.information_ratio
                        if r.rolling_ic is not None
                        else None
                    ),
                    perm_p=r.permutation_p_value,
                    n=r.n_samples,
                    horizon=r.forward_bars,
                    regime=r.regime,
                    indicator=key,
                )

    # 分类
    robust: List[CrossTFSignal] = []
    divergent: List[CrossTFSignal] = []
    tf_specific: List[CrossTFSignal] = []

    for regime_key, tf_entries in signal_map.items():
        tf_scores = [
            TFScore(
                timeframe=tf,
                ic=entry.ic,
                ir=entry.ir,
                permutation_p=entry.perm_p,
                n_samples=entry.n,
                forward_bars=entry.horizon,
            )
            for tf, entry in sorted(tf_entries.items())
        ]

        n_tfs = len(tf_scores)
        ics = [s.ic for s in tf_scores]
        irs = [s.ir for s in tf_scores if s.ir is not None]
        avg_abs_ic = sum(abs(ic) for ic in ics) / len(ics)
        avg_ir = sum(irs) / len(irs) if irs else 0.0
        direction_consistent = all(ic > 0 for ic in ics) or all(ic < 0 for ic in ics)

        # 推荐 TF：综合 IC 强度 × IR 稳定性 × 排列检验
        best_tf = _recommend_tf(tf_scores)

        # 从 key 中提取 indicator 和 regime
        first_entry = next(iter(tf_entries.values()))
        indicator = first_entry.indicator
        regime = first_entry.regime

        if n_tfs >= min_tfs_for_robust and direction_consistent:
            robustness = "robust"
        elif n_tfs >= min_tfs_for_robust:
            robustness = "tf_divergent"
        else:
            robustness = "tf_specific"

        signal = CrossTFSignal(
            indicator=indicator,
            regime=regime,
            tf_scores=tf_scores,
            n_tfs=n_tfs,
            avg_abs_ic=avg_abs_ic,
            avg_ir=avg_ir,
            direction_consistent=direction_consistent,
            recommended_tf=best_tf,
            robustness=robustness,
        )

        if robustness == "robust":
            robust.append(signal)
        elif robustness == "tf_divergent":
            divergent.append(signal)
        else:
            tf_specific.append(signal)

    robust.sort(key=lambda s: s.avg_abs_ic, reverse=True)
    divergent.sort(key=lambda s: s.avg_abs_ic, reverse=True)
    tf_specific.sort(key=lambda s: s.avg_abs_ic, reverse=True)

    # TF 推荐汇总
    tf_recs: Dict[str, List[str]] = {tf: [] for tf in timeframes}
    for sig in robust:
        tf_recs[sig.recommended_tf].append(
            f"{sig.indicator}[{sig.regime}]" if sig.regime else sig.indicator
        )

    return CrossTFAnalysis(
        timeframes=timeframes,
        robust_signals=robust,
        divergent_signals=divergent,
        tf_specific_signals=tf_specific[:20],
        tf_recommendations=tf_recs,
    )


def _recommend_tf(scores: List[TFScore]) -> str:
    """综合评分选最佳 TF。

    score = |IC| × (1 + IR_bonus) × perm_bonus × sample_bonus
    """
    best_tf = scores[0].timeframe
    best_score = float("-inf")

    for s in scores:
        ic_score = abs(s.ic)
        ir_bonus = max(0, (s.ir or 0) * 0.3)  # IR > 0 有加分
        perm_bonus = (
            1.2 if (s.permutation_p is not None and s.permutation_p < 0.05) else 1.0
        )
        sample_bonus = min(1.0, s.n_samples / 200.0)  # n≥200 满分

        total = ic_score * (1 + ir_bonus) * perm_bonus * sample_bonus
        if total > best_score:
            best_score = total
            best_tf = s.timeframe

    return best_tf


@dataclass
class _BestEntry:
    ic: float
    ir: Optional[float]
    perm_p: Optional[float]
    n: int
    horizon: int
    regime: Optional[str]
    indicator: str
