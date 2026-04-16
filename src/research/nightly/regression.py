"""Nightly 报告同比回归检测。

纯函数：接受两个 NightlyWFReport（current + previous），返回 RegressionFinding 元组。

阈值语义（config 侧约定）：
  - sharpe_regression_threshold = 0.20 表示 Sharpe 下降 > 20% 视为 regression
  - dd_regression_threshold = 0.15 表示 max_dd 恶化 > 15% 视为 regression
"""

from __future__ import annotations

from typing import Dict, Tuple

from .contracts import (
    NightlyWFConfig,
    NightlyWFReport,
    RegressionFinding,
    StrategyMetrics,
)


def compare_reports(
    *,
    current: NightlyWFReport,
    previous: NightlyWFReport,
    config: NightlyWFConfig,
) -> Tuple[RegressionFinding, ...]:
    """比较两份报告，返回回归/改善/持平清单。

    规则：
      - Sharpe：change_ratio = (cur - prev) / max(abs(prev), 1e-9)
        下降（ratio < -sharpe_regression_threshold）→ regression
        上升（ratio > +threshold）→ improvement
        其他 → unchanged
      - max_dd：越小越好。change_ratio 同上但符号相反
    """
    prev_map: Dict[Tuple[str, str], StrategyMetrics] = {
        (m.strategy, m.timeframe): m for m in previous.metrics
    }
    findings: list[RegressionFinding] = []

    for cur in current.metrics:
        key = (cur.strategy, cur.timeframe)
        prev = prev_map.get(key)
        if prev is None:
            continue  # 新策略/新 TF：无同比基线

        # Sharpe 同比
        prev_sharpe = prev.sharpe
        denom_s = max(abs(prev_sharpe), 1e-9)
        ratio_s = (cur.sharpe - prev_sharpe) / denom_s
        findings.append(
            RegressionFinding(
                strategy=cur.strategy,
                timeframe=cur.timeframe,
                metric="sharpe",
                previous=prev_sharpe,
                current=cur.sharpe,
                change_ratio=ratio_s,
                severity=_classify(ratio_s, config.sharpe_regression_threshold),
            )
        )

        # Max DD 同比（越小越好 → 升高即恶化）
        prev_dd = prev.max_dd
        denom_d = max(prev_dd, 1e-9)
        ratio_d = (cur.max_dd - prev_dd) / denom_d
        findings.append(
            RegressionFinding(
                strategy=cur.strategy,
                timeframe=cur.timeframe,
                metric="max_dd",
                previous=prev_dd,
                current=cur.max_dd,
                change_ratio=ratio_d,
                # DD 升高 = 恶化，符号与 Sharpe 相反
                severity=_classify(-ratio_d, config.dd_regression_threshold),
            )
        )

    return tuple(findings)


def _classify(change_ratio_positive_is_good: float, threshold: float) -> str:
    """ratio > +threshold → improvement；ratio < -threshold → regression；else unchanged。"""
    if change_ratio_positive_is_good < -threshold:
        return "regression"
    if change_ratio_positive_is_good > threshold:
        return "improvement"
    return "unchanged"
