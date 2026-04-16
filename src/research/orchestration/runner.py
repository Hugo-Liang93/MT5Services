"""MiningRunner — 信号挖掘编排器。

纯数据驱动：历史数据 + 指标值 → 统计发现。
不涉及任何现有策略的评估——职责边界：发现，不验证。

加载数据 → 构建 DataMatrix → 分发给分析器 → 汇总结果。
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.backtesting.filtering.economic import load_backtest_economic_events
from src.calendar.economic_calendar.gold_relevance import (
    EventRelevanceMatcher,
    EventSummary,
    GoldRelevancePolicy,
    build_relevance_matcher,
)
from src.calendar.economic_calendar.trade_guard import infer_symbol_context
from src.config.centralized import get_economic_config
from src.config.database import load_db_settings
from src.config.models.runtime import EconomicConfig
from src.persistence.db import TimescaleWriter
from src.persistence.repositories.economic_repo import EconomicCalendarRepository
from src.research.core.config import ResearchConfig, load_research_config
from src.research.core.contracts import DataSummary, Finding, MiningResult
from src.research.core.data_matrix import DataMatrix, build_data_matrix

logger = logging.getLogger(__name__)


def _build_relevance_matcher(
    settings: EconomicConfig,
) -> Optional[EventRelevanceMatcher]:
    """从 EconomicConfig 显式构造相关性匹配器。

    契约：
      - `trade_guard_relevance_filter_enabled = False` → 返回 None（不过滤）
      - 启用 + keywords/categories 至少一项非空 → 返回 matcher
      - 启用但两项均空 → `build_relevance_matcher` raise ValueError（配置矛盾）
    """
    if not settings.trade_guard_relevance_filter_enabled:
        return None
    policy = GoldRelevancePolicy.from_csv(
        keywords_csv=settings.gold_impact_keywords,
        categories_csv=settings.gold_impact_categories,
    )
    return build_relevance_matcher(policy)


def _load_high_impact_event_times(
    *,
    symbol: str,
    start_time: datetime,
    end_time: datetime,
) -> Tuple[datetime, ...]:
    """加载回测窗口的高影响经济事件时间戳（供事件距离特征使用）。

    流程（失败即抛出，不兜底）：
      1. 读 EconomicConfig 拿 importance 阈值 + 构造相关性 matcher
      2. 按 symbol 推导相关货币，从 DB 拉 importance ≥ 阈值的事件
      3. 若启用相关性过滤，对事件走 matcher；未命中者剔除
      4. 返回排序后的 UTC 时间戳元组

    失败模式（均直接抛出，不静默兜底）：
      - DB 连接失败 → psycopg2 错误直抛
      - EconomicConfig 缺字段 → AttributeError 直抛（配置 bug 应尽早暴露）
      - relevance_filter_enabled=True 但 keyword/category 都为空 → ValueError（配置矛盾）
    """
    settings = get_economic_config()
    importance_min = int(settings.high_importance_threshold)
    matcher = _build_relevance_matcher(settings)

    db_settings = load_db_settings()
    writer = TimescaleWriter(db_settings, min_conn=1, max_conn=2)
    try:
        repo = EconomicCalendarRepository(writer)
        context = infer_symbol_context(symbol)
        events = load_backtest_economic_events(
            economic_repo=repo,
            start_time=start_time,
            end_time=end_time,
            currencies=context["currencies"] or None,
            importance_min=importance_min,
        )
    finally:
        writer.close()

    if matcher is not None:
        events = [ev for ev in events if matcher.is_relevant(_event_to_summary(ev))]

    times = tuple(sorted(ev.scheduled_at for ev in events))
    logger.info(
        "Research mining: loaded %d high-impact events "
        "(symbol=%s, imp>=%d, relevance_filter=%s)",
        len(times),
        symbol,
        importance_min,
        matcher is not None,
    )
    return times


def _event_to_summary(ev: Any) -> EventSummary:
    """_SimpleEvent → EventSummary 契约转换（单一职责）。"""
    return EventSummary(
        name=ev.event_name,
        category=ev.category if ev.category else None,
    )


class MiningRunner:
    """信号挖掘运行器。

    职责边界：纯数据挖掘（指标值 → 未来收益的统计关系）。
    不评估现有策略，不涉及 SignalModule。
    """

    def __init__(
        self,
        config: Optional[ResearchConfig] = None,
        components: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._config = config or load_research_config()
        self._components = components

        from src.research.features.hub import FeatureHub

        self._feature_hub = FeatureHub(self._config)

    def run(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        *,
        analyses: Optional[List[str]] = None,
        indicator_filter: Optional[List[str]] = None,
        child_tf: str = "",
    ) -> MiningResult:
        """执行信号挖掘。

        Args:
            symbol: 交易品种
            timeframe: 时间框架
            start_time: 数据起始时间
            end_time: 数据结束时间
            analyses: 要执行的分析类型
                      ["predictive_power", "threshold"]
                      None = 全部
            indicator_filter: 仅分析这些指标（用于 threshold 分析）
            child_tf: 子 TF（如 "M5"），非空时加载子 TF bars 供 intrabar 特征使用

        Returns:
            MiningResult
        """
        run_id = f"mine_{uuid.uuid4().hex[:12]}"
        started_at = datetime.utcnow()

        if analyses is None:
            analyses = ["predictive_power", "threshold", "rule_mining"]

        # 构建组件（复用 backtesting 基础设施的数据加载 + 指标计算）
        if self._components is None:
            from src.backtesting.component_factory import build_backtest_components

            self._components = build_backtest_components()

        # 加载回测期间的高影响经济事件（供派生事件特征使用）
        high_impact_events = _load_high_impact_event_times(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
        )

        # 构建 DataMatrix
        matrix = build_data_matrix(
            symbol=symbol,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            forward_horizons=self._config.forward_horizons,
            warmup_bars=self._config.warmup_bars,
            train_ratio=self._config.train_ratio,
            round_trip_cost_pct=self._config.round_trip_cost_pct,
            components=self._components,
            high_impact_event_times=high_impact_events,
            child_tf=child_tf,
        )

        # 特征计算（FeatureHub 统一调度所有 Provider）
        extra_data = None
        extra_reqs = self._feature_hub.required_extra_data()
        if extra_reqs:
            extra_data = self._prepare_extra_data(
                symbol, timeframe, start_time, end_time, extra_reqs
            )

        compute_result = self._feature_hub.compute_all(matrix, extra_data)
        logger.info(
            "FeatureHub: %d features across %d providers",
            compute_result.total_features,
            len(compute_result.provider_summaries),
        )

        # provider_groups 供分析器分组 FDR
        provider_groups = self._feature_hub.feature_names_by_provider()

        # 数据摘要
        regime_dist = Counter(r.value for r in matrix.regimes)
        data_summary = DataSummary(
            symbol=symbol,
            timeframe=timeframe,
            n_bars=matrix.n_bars,
            start_time=matrix.bar_times[0] if matrix.bar_times else start_time,
            end_time=matrix.bar_times[-1] if matrix.bar_times else end_time,
            train_bars=len(matrix.train_slice()),
            test_bars=len(matrix.test_slice()),
            regime_distribution=dict(regime_dist),
            available_indicators=[
                f"{ind}.{fld}" for ind, fld in matrix.available_indicator_fields()
            ],
        )

        result = MiningResult(
            run_id=run_id,
            started_at=started_at,
            data_summary=data_summary,
        )

        # 执行分析器
        if "predictive_power" in analyses:
            result.predictive_power = self._run_predictive_power(
                matrix, provider_groups
            )

        if "threshold" in analyses:
            result.threshold_sweeps = self._run_threshold_sweep(
                matrix,
                indicator_filter,
            )

        if "rule_mining" in analyses:
            result.mined_rules = self._run_rule_mining(matrix, provider_groups)

        # 汇总 Top Findings
        result.top_findings = self._rank_findings(result)

        # 按 Provider 分组 Findings
        result.findings_by_provider = self._group_findings_by_provider(
            result.top_findings, provider_groups
        )

        # 跨 Provider 规则
        if result.mined_rules and provider_groups:
            from src.research.analyzers.rule_mining import tag_cross_provider_rules

            result.cross_provider_rules = tag_cross_provider_rules(
                result.mined_rules, provider_groups
            )

        # 特征计算摘要
        result.feature_compute_summary = compute_result.to_dict()

        result.completed_at = datetime.utcnow()

        return result

    def _run_predictive_power(
        self,
        matrix: DataMatrix,
        provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
    ) -> list:
        from src.research.analyzers.predictive_power import analyze_predictive_power

        return analyze_predictive_power(
            matrix,
            config=self._config.predictive_power,
            overfitting_config=self._config.overfitting,
            provider_groups=provider_groups,
            fdr_grouping=self._config.feature_providers.fdr_grouping,
        )

    def _run_threshold_sweep(
        self,
        matrix: DataMatrix,
        indicator_filter: Optional[List[str]],
    ) -> list:
        from src.research.analyzers.threshold import analyze_thresholds

        # 确定要扫描的指标
        if indicator_filter:
            fields = [
                (ind, fld)
                for ind, fld in matrix.available_indicator_fields()
                if ind in indicator_filter
            ]
        else:
            # 默认扫描核心振荡类指标的主要字段
            _DEFAULT_FIELDS = {
                "rsi14": "rsi",
                "adx14": "adx",
                "atr14": "atr",
                "cci20": "cci",
                "stoch_rsi14": "stoch_rsi_k",
                "williams_r14": "williams_r",
                "macd": "hist",
                "roc12": "roc",
            }
            fields = [
                (ind, fld)
                for ind, fld in matrix.available_indicator_fields()
                if ind in _DEFAULT_FIELDS and fld == _DEFAULT_FIELDS[ind]
            ]
            if not fields:
                fields = matrix.available_indicator_fields()

        # Regime 分层：None = 全部混合，再加各 regime 单独扫描
        regime_filters: List[Optional[str]] = [None]
        if self._config.threshold_sweep.per_regime:
            regime_dist = Counter(r.value for r in matrix.regimes)
            for regime_val, count in regime_dist.items():
                if count >= self._config.overfitting.min_samples:
                    regime_filters.append(regime_val)

        results = []
        for regime_filter in regime_filters:
            for ind_name, field_name in fields:
                sweep = analyze_thresholds(
                    matrix,
                    ind_name,
                    field_name,
                    config=self._config.threshold_sweep,
                    overfitting_config=self._config.overfitting,
                    regime_filter=regime_filter,
                )
                results.extend(sweep)

        return results

    def _run_rule_mining(
        self,
        matrix: DataMatrix,
        provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
    ) -> list:
        from src.research.analyzers.rule_mining import RuleMiningConfig as _RMCfg
        from src.research.analyzers.rule_mining import mine_rules

        # 从 ResearchConfig.rule_mining 构建 RuleMiningConfig
        rm = self._config.rule_mining
        cfg = _RMCfg(
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
            overfitting_config=self._config.overfitting,
        )

    def _prepare_extra_data(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        extra_reqs: list,
    ) -> None:
        """准备跨 TF 数据。当前 CrossTF 默认关闭，此方法为扩展预留。"""
        logger.warning(
            "CrossTF extra_data preparation not yet implemented; "
            "cross_tf provider will produce no features"
        )
        return None

    def _group_findings_by_provider(
        self,
        findings: List[Finding],
        provider_groups: Dict[str, List[Tuple[str, str]]],
    ) -> Dict[str, List[Finding]]:
        """按 Provider 分组 Findings。"""
        if not provider_groups:
            return {}

        # 构建 (indicator, field) → provider 映射
        key_to_provider: Dict[Tuple[str, str], str] = {}
        for prov, keys in provider_groups.items():
            for k in keys:
                key_to_provider[k] = prov

        grouped: Dict[str, List[Finding]] = {}
        for f in findings:
            # 从 summary 中提取 indicator.field（格式：indicator.field IC=...）
            indicator_part = f.summary.split()[0] if f.summary else ""
            if "." in indicator_part:
                parts = indicator_part.split(".", 1)
                key = (parts[0], parts[1])
                prov = key_to_provider.get(key, "base")
            else:
                prov = "base"
            grouped.setdefault(prov, []).append(f)

        return grouped

    def _rank_findings(self, result: MiningResult) -> List[Finding]:
        """从各分析器结果中提取 Top Findings，按显著性排名。"""
        findings: List[Finding] = []

        # 从 Predictive Power 提取显著发现
        for pp in result.predictive_power:
            if not pp.is_significant:
                continue
            ic = pp.information_coefficient
            hit_dev = max(
                abs(pp.hit_rate_above_median - 0.5),
                abs(pp.hit_rate_below_median - 0.5),
            )
            score = abs(ic) * (1 + hit_dev * 10)

            regime_str = f" ({pp.regime})" if pp.regime else ""
            above_pct = pp.hit_rate_above_median * 100
            below_pct = pp.hit_rate_below_median * 100

            findings.append(
                Finding(
                    rank=0,
                    category="predictive_power",
                    summary=(
                        f"{pp.indicator_name}.{pp.field_name} "
                        f"IC={ic:+.3f} "
                        f"hit_above={above_pct:.1f}%/below={below_pct:.1f}% "
                        f"({pp.forward_bars}-bar{regime_str}, n={pp.n_samples})"
                    ),
                    confidence_level=(
                        "high"
                        if pp.n_samples >= 100 and pp.p_value < 0.01
                        else "medium"
                    ),
                    significance_score=score,
                    action=f"考虑在{regime_str or '全部 regime'}中使用此指标构建策略",
                )
            )

        # 从 Threshold Sweep 提取
        for ts in result.threshold_sweeps:
            for side, threshold, hr, n, cv, test_hr in [
                (
                    "buy",
                    ts.optimal_buy_threshold,
                    ts.buy_hit_rate,
                    ts.buy_n_signals,
                    ts.cv_consistency_buy,
                    ts.test_buy_hit_rate,
                ),
                (
                    "sell",
                    ts.optimal_sell_threshold,
                    ts.sell_hit_rate,
                    ts.sell_n_signals,
                    ts.cv_consistency_sell,
                    ts.test_sell_hit_rate,
                ),
            ]:
                if threshold is None or n < 10:
                    continue
                score = (hr - 0.5) * cv * n / 100
                if score <= 0:
                    continue

                regime_str = f" ({ts.regime})" if ts.regime else ""
                test_str = f", test={test_hr * 100:.1f}%" if test_hr is not None else ""
                fragile = " fragile" if cv < 0.6 else ""

                findings.append(
                    Finding(
                        rank=0,
                        category="threshold",
                        summary=(
                            f"{ts.indicator_name}.{ts.field_name} "
                            f"{side}@{threshold:.2f} "
                            f"hit={hr * 100:.1f}%{test_str} "
                            f"CV={cv:.0%}{fragile} "
                            f"({ts.forward_bars}-bar{regime_str}, n={n})"
                        ),
                        confidence_level=(
                            "high"
                            if cv >= 0.8 and test_hr and test_hr > 0.55
                            else "medium" if cv >= 0.6 else "low"
                        ),
                        significance_score=score,
                        action=(
                            f"建议将 {ts.indicator_name} {side} "
                            f"阈值调整为 {threshold:.2f}"
                        ),
                    )
                )

        # 从 Rule Mining 提取
        for rule in result.mined_rules:
            test_hr = rule.test_hit_rate or 0.0
            score = (test_hr - 0.5) * rule.test_n_samples / 10
            if score <= 0:
                continue

            regime_str = f" ({rule.regime})" if rule.regime else ""
            test_str = (
                f" test={test_hr * 100:.1f}%/{rule.test_n_samples}"
                if rule.test_hit_rate is not None
                else ""
            )

            findings.append(
                Finding(
                    rank=0,
                    category="rule",
                    summary=(
                        f"{rule.rule_string()} "
                        f"train={rule.train_hit_rate * 100:.1f}%/{rule.train_n_samples}"
                        f"{test_str}{regime_str}"
                    ),
                    confidence_level=(
                        "high"
                        if test_hr >= 0.58 and rule.test_n_samples >= 30
                        else "medium" if test_hr >= 0.52 else "low"
                    ),
                    significance_score=score,
                    action=rule.rule_string(),
                )
            )

        # 按 score 降序排名
        findings.sort(key=lambda f: f.significance_score, reverse=True)
        ranked: List[Finding] = []
        for i, f in enumerate(findings[:30]):  # Top 30
            ranked.append(
                Finding(
                    rank=i + 1,
                    category=f.category,
                    summary=f.summary,
                    confidence_level=f.confidence_level,
                    significance_score=f.significance_score,
                    action=f.action,
                    detail=f.detail,
                )
            )

        return ranked
