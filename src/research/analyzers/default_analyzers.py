"""3 个内置 Analyzer 实现 — Protocol 包装现有 `analyze_xxx` / `mine_rules` 函数。

每个 Analyzer 类自包含：
  - 名称 / 字段 / FDR 需求声明
  - analyze() 内含 analyzer-specific 准备逻辑（如 threshold 的 fields 选择 +
    regime 分层循环、rule_mining 的 RuleMiningConfig 构建）

之前这些逻辑散落在 `orchestration/runner.py` 的 `_run_xxx` 私有方法里，
导致 runner 知道每个 analyzer 的细节。P2 重构后 runner 只负责"派发 + 写
结果到 typed 字段"，analyzer-specific 全部回归 analyzer 自身。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .barrier_predictive_power import analyze_barrier_predictive_power
from .multi_tf_aggregator import analyze_cross_tf  # noqa: F401  (公开导出)
from .predictive_power import analyze_predictive_power
from .protocol import register_analyzer
from .rule_mining import RuleMiningConfig, mine_rules
from .threshold import analyze_thresholds


class PredictivePowerAnalyzer:
    """指标预测力分析器（IC + 排列检验 + Rolling IR）。"""

    name = "predictive_power"
    result_field = "predictive_power"
    requires_provider_groups = True

    def analyze(
        self,
        matrix: Any,
        *,
        config: Any,
        provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
        indicator_filter: Optional[List[str]] = None,
    ) -> List[Any]:
        return analyze_predictive_power(
            matrix,
            config=config.predictive_power,
            overfitting_config=config.overfitting,
            provider_groups=provider_groups,
            fdr_grouping=config.feature_providers.fdr_grouping,
        )


class ThresholdSweepAnalyzer:
    """阈值扫描分析器（最优买卖阈值 + CV + 排列检验）。"""

    name = "threshold"
    result_field = "threshold_sweeps"
    requires_provider_groups = False

    # 默认扫描的核心振荡类指标
    _DEFAULT_FIELDS: Dict[str, str] = {
        "rsi14": "rsi",
        "adx14": "adx",
        "atr14": "atr",
        "cci20": "cci",
        "stoch_rsi14": "stoch_rsi_k",
        "williams_r14": "williams_r",
        "macd": "hist",
        "roc12": "roc",
    }

    def analyze(
        self,
        matrix: Any,
        *,
        config: Any,
        provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
        indicator_filter: Optional[List[str]] = None,
    ) -> List[Any]:
        ts_cfg = config.threshold_sweep
        of_cfg = config.overfitting

        # 选择要扫描的字段
        if indicator_filter:
            fields = [
                (ind, fld)
                for ind, fld in matrix.available_indicator_fields()
                if ind in indicator_filter
            ]
        else:
            fields = [
                (ind, fld)
                for ind, fld in matrix.available_indicator_fields()
                if ind in self._DEFAULT_FIELDS and fld == self._DEFAULT_FIELDS[ind]
            ]
            if not fields:
                fields = matrix.available_indicator_fields()

        # Regime 分层
        regime_filters: List[Optional[str]] = [None]
        if ts_cfg.per_regime:
            regime_dist = Counter(r.value for r in matrix.regimes)
            for regime_val, count in regime_dist.items():
                if count >= of_cfg.min_samples:
                    regime_filters.append(regime_val)

        results: List[Any] = []
        for regime_filter in regime_filters:
            for ind_name, field_name in fields:
                sweep = analyze_thresholds(
                    matrix,
                    ind_name,
                    field_name,
                    config=ts_cfg,
                    overfitting_config=of_cfg,
                    regime_filter=regime_filter,
                )
                results.extend(sweep)
        return results


class BarrierPredictivePowerAnalyzer:
    """Barrier 预测力分析器 — IC 基于 Triple-Barrier 真实出场收益。

    与 PredictivePowerAnalyzer 的关系：
      - predictive_power: IC 基于朴素 N-bar forward_return（定点观测）
      - barrier_predictive_power: IC 基于 tp/sl/time barrier 出场收益（与实盘同构）
    两者共存，提供两种语义下的预测力证据。
    """

    name = "barrier_predictive_power"
    result_field = "barrier_predictive_power"
    requires_provider_groups = False

    def analyze(
        self,
        matrix: Any,
        *,
        config: Any,
        provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
        indicator_filter: Optional[List[str]] = None,
    ) -> List[Any]:
        # indicator_filter 语义：若指定，仅对其对应的无量纲字段分析
        fields: Optional[List[Tuple[str, str]]] = None
        if indicator_filter:
            fields = [
                (ind, fld)
                for ind, fld in matrix.available_indicator_fields()
                if ind in indicator_filter
            ]
        return analyze_barrier_predictive_power(
            matrix,
            indicator_fields=fields,
            config=config.predictive_power,
            overfitting_config=config.overfitting,
        )


class RuleMiningAnalyzer:
    """规则挖掘分析器（决策树多条件 IF-THEN 规则）。"""

    name = "rule_mining"
    result_field = "mined_rules"
    requires_provider_groups = True  # 用于 cross_provider 标注

    def analyze(
        self,
        matrix: Any,
        *,
        config: Any,
        provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
        indicator_filter: Optional[List[str]] = None,
    ) -> List[Any]:
        rm = config.rule_mining
        cfg = RuleMiningConfig(
            max_depth=rm.max_depth,
            min_samples_leaf=rm.min_samples_leaf,
            min_hit_rate=rm.min_hit_rate,
            min_test_hit_rate=rm.min_test_hit_rate,
            max_rules=rm.max_rules,
            dimensionless_only=rm.dimensionless_only,
            n_permutations=rm.n_permutations,
            permutation_significance=rm.permutation_significance,
            cv_folds=rm.cv_folds,
            cv_consistency_threshold=rm.cv_consistency_threshold,
        )
        return mine_rules(
            matrix,
            config=cfg,
            overfitting_config=config.overfitting,
        )


def register_default_analyzers() -> None:
    """注册 4 个内置 analyzer（按默认执行顺序）。"""
    register_analyzer(PredictivePowerAnalyzer())
    register_analyzer(BarrierPredictivePowerAnalyzer())
    register_analyzer(ThresholdSweepAnalyzer())
    register_analyzer(RuleMiningAnalyzer())
