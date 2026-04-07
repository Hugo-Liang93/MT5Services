from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from .analytics import (
    DiagnosticsEngine,
    DiagnosticThresholds,
    SignalDiagnosticsAnalyzer,
)
from .evaluation.calibrator import ConfidenceCalibrator
from .evaluation.performance import StrategyPerformanceTracker
from .evaluation.regime import MarketRegimeDetector, RegimeType, SoftRegimeResult
from .metadata_keys import MetadataKey as MK
from .models import SignalContext, SignalDecision, SignalRecord
from .strategies.adapters import IndicatorSource
from .strategies.base import SignalStrategy
from .strategies.catalog import build_default_strategy_set
from .tracking.repository import SignalRepository

logger = logging.getLogger(__name__)


class SignalModule:
    def __init__(
        self,
        indicator_source: IndicatorSource,
        strategies: Optional[Iterable[SignalStrategy]] = None,
        repository: Optional[SignalRepository] = None,
        regime_detector: Optional[MarketRegimeDetector] = None,
        calibrator: Optional[ConfidenceCalibrator] = None,
        performance_tracker: Optional[StrategyPerformanceTracker] = None,
        diagnostics_engine: Optional[DiagnosticsEngine] = None,
        soft_regime_enabled: bool = False,
        confidence_floor: float = 0.10,
        confidence_floor_min_affinity: float = 0.15,
    ):
        self.indicator_source = indicator_source
        self.repository = repository
        self._regime_detector: MarketRegimeDetector = (
            regime_detector or MarketRegimeDetector()
        )
        self._calibrator: Optional[ConfidenceCalibrator] = calibrator
        self._performance_tracker: Optional[StrategyPerformanceTracker] = (
            performance_tracker
        )
        self._soft_regime_enabled = bool(soft_regime_enabled)
        self._confidence_floor = max(0.0, float(confidence_floor))
        self._confidence_floor_min_affinity = max(
            0.0, float(confidence_floor_min_affinity)
        )
        self._diagnostics_analyzer: DiagnosticsEngine = (
            diagnostics_engine or SignalDiagnosticsAnalyzer()
        )
        self._strategies: dict[str, SignalStrategy] = {}
        # Cache strategy regime affinity metadata to avoid repeated getattr in hot paths.
        self._strategy_affinity_cache: dict[str, dict] = {}
        default_strategies: Iterable[SignalStrategy] = (
            list(strategies) if strategies is not None else build_default_strategy_set()
        )
        for strategy in default_strategies:
            self.register_strategy(strategy)

    @staticmethod
    def _validate_strategy_attrs(strategy: SignalStrategy) -> None:
        """S-1: Validate the required strategy class attributes at registration time.

        Raise ``AttributeError`` early instead of failing silently in the runtime hot path.
        """
        name = getattr(strategy, "name", None)
        if not name or not isinstance(name, str):
            raise AttributeError(
                f"Strategy {type(strategy).__name__} must define a non-empty string "
                f"class attribute 'name'."
            )
        required_indicators = getattr(strategy, "required_indicators", None)
        if required_indicators is None:
            raise AttributeError(
                f"Strategy '{name}' must define 'required_indicators' "
                f"(tuple of indicator names, or empty tuple)."
            )
        preferred_scopes = getattr(strategy, "preferred_scopes", None)
        if preferred_scopes is None:
            raise AttributeError(
                f"Strategy '{name}' must define 'preferred_scopes' "
                f'(e.g. ("confirmed",) or ("intrabar", "confirmed")).'
            )
        regime_affinity = getattr(strategy, "regime_affinity", None)
        if regime_affinity is None:
            raise AttributeError(
                f"Strategy '{name}' must define 'regime_affinity' "
                f"(Dict[RegimeType, float] covering TRENDING/RANGING/BREAKOUT/UNCERTAIN). "
                f"See CLAUDE.md for the Regime affinity design guide."
            )
        from .evaluation.regime import RegimeType as _RT

        _required_regimes = {_RT.TRENDING, _RT.RANGING, _RT.BREAKOUT, _RT.UNCERTAIN}
        missing = _required_regimes - set(regime_affinity.keys())
        if missing:
            missing_names = sorted(r.value for r in missing)
            raise AttributeError(
                f"Strategy '{name}' regime_affinity missing keys: {missing_names}. "
                f"All 4 RegimeType values must be covered."
            )
        # category 校验：必须是 StrategyCategory 枚举值或对应的字符串
        from .strategies.base import StrategyCategory

        category = getattr(strategy, "category", None)
        if category is not None:
            valid_values = {c.value for c in StrategyCategory}
            cat_str = str(
                category.value if isinstance(category, StrategyCategory) else category
            )
            if cat_str not in valid_values:
                raise AttributeError(
                    f"Strategy '{name}' has invalid category '{cat_str}'. "
                    f"Valid values: {sorted(valid_values)}"
                )

        # htf_policy 校验（StructuredStrategyBase 子类必须声明）
        from .strategies.structured.base import HtfPolicy, StructuredStrategyBase

        if isinstance(strategy, StructuredStrategyBase):
            htf_policy = getattr(strategy, "htf_policy", None)
            if htf_policy is None or not isinstance(htf_policy, HtfPolicy):
                raise AttributeError(
                    f"Structured strategy '{name}' must declare 'htf_policy' "
                    f"as HtfPolicy enum (HARD_GATE/SOFT_GATE/SOFT_BONUS/NONE). "
                    f"See docs/signal-system.md §策略级 HTF 分层."
                )

    def register_strategy(self, strategy: SignalStrategy) -> None:
        self._validate_strategy_attrs(strategy)
        self._strategies[strategy.name] = strategy
        if self._performance_tracker is not None:
            raw_cat = getattr(strategy, "category", "unknown")
            category = str(raw_cat.value if hasattr(raw_cat, "value") else raw_cat)
            self._performance_tracker.register_strategy(strategy.name, category)

    def intrabar_required_indicators(self) -> frozenset:
        """从策略的 preferred_scopes 自动推导哪些指标需要 intrabar 计算。

        遍历所有已注册策略，收集 preferred_scopes 包含 "intrabar" 的策略的
        required_indicators 并集。返回值注入到 UnifiedIndicatorManager，
        后者据此决定 intrabar pipeline 计算哪些指标。
        """
        result: set[str] = set()
        for strategy in self._strategies.values():
            scopes = getattr(strategy, "preferred_scopes", ("intrabar", "confirmed"))
            if "intrabar" not in scopes:
                continue
            for ind in getattr(strategy, "required_indicators", ()):
                result.add(str(ind))
        return frozenset(result)

    def apply_param_overrides(
        self,
        strategy_params: Dict[str, Any],
        regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
        *,
        strategy_params_per_tf: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> None:
        """构建 TFParamResolver 并注入各策略 + 应用 regime_affinity 覆盖。

        Args:
            strategy_params: 全局默认 ``{"supertrend__adx_threshold": 23.0}``
            regime_affinity_overrides: ``{"supertrend": {"trending": 1.0}}``
            strategy_params_per_tf: per-TF 覆盖 ``{"M5": {"rsi__overbought": 72.0}}``
        """
        from src.signals.strategies.tf_params import build_tf_param_resolver

        resolver = build_tf_param_resolver(
            strategy_params, strategy_params_per_tf or {}
        )
        self._tf_param_resolver = resolver  # type: ignore[attr-defined]
        for strategy in self._strategies.values():
            strategy._tf_param_resolver = resolver  # type: ignore[attr-defined]

        if regime_affinity_overrides:
            _regime_map = {
                "trending": RegimeType.TRENDING,
                "ranging": RegimeType.RANGING,
                "breakout": RegimeType.BREAKOUT,
                "uncertain": RegimeType.UNCERTAIN,
            }
            for strategy_name, affinity_dict in regime_affinity_overrides.items():
                strategy = self._strategies.get(strategy_name)
                if strategy is None:
                    continue
                for regime_key, weight in affinity_dict.items():
                    regime_type = _regime_map.get(regime_key.lower())
                    if regime_type is not None and hasattr(strategy, "regime_affinity"):
                        strategy.regime_affinity[regime_type] = weight

    def get_strategy(self, name: str) -> Optional["SignalStrategy"]:
        """Return strategy instance by name, or None if not registered."""
        return self._strategies.get(name)

    def list_strategies(self) -> list[str]:
        return sorted(self._strategies.keys())

    def list_intrabar_strategies(self) -> list[str]:
        """返回 preferred_scopes 包含 intrabar 的策略名列表。"""
        return sorted(
            name
            for name, s in self._strategies.items()
            if "intrabar" in getattr(s, "preferred_scopes", ())
        )

    def strategy_requirements(self, strategy: str) -> tuple[str, ...]:
        strategy_impl = self._strategies.get(strategy)
        if strategy_impl is None:
            raise ValueError(f"unsupported signal strategy: {strategy}")
        requirements = getattr(strategy_impl, "required_indicators", ())
        return tuple(str(item) for item in requirements)

    def strategy_category(self, strategy: str) -> str:
        impl = self._strategies.get(strategy)
        raw = getattr(impl, "category", "") if impl else ""
        return str(raw.value if hasattr(raw, "value") else raw)

    def strategy_affinity_map(self, strategy: str) -> Dict[RegimeType, float]:
        """返回策略的 regime_affinity 字典（不存在时返回空字典）。

        运行时在启动时缓存此结果，避免每次 process_next_event 重复 getattr。
        """
        impl = self._strategies.get(strategy)
        return getattr(impl, "regime_affinity", {}) if impl else {}

    @property
    def soft_regime_enabled(self) -> bool:
        return self._soft_regime_enabled

    def effective_regime_affinity(
        self,
        strategy: str,
        *,
        regime: Optional[RegimeType] = None,
        soft_regime: Optional[SoftRegimeResult | Dict[str, Any]] = None,
    ) -> float:
        affinity_map = self.strategy_affinity_map(strategy)
        return self._effective_affinity(affinity_map, regime, soft_regime)

    def strategy_scopes(self, strategy: str) -> tuple[str, ...]:
        """Return the snapshot scopes this strategy wants to receive.

        Reads ``preferred_scopes`` from the strategy class.  Falls back to
        ``("intrabar", "confirmed")`` for strategies that pre-date this field
        so that existing behaviour is preserved by default.
        """
        strategy_impl = self._strategies.get(strategy)
        if strategy_impl is None:
            raise ValueError(f"unsupported signal strategy: {strategy}")
        scopes = getattr(strategy_impl, "preferred_scopes", ("intrabar", "confirmed"))
        return tuple(str(s) for s in scopes)

    def describe_strategy(self, strategy: str) -> dict[str, Any]:
        if strategy not in self._strategies:
            raise ValueError(f"unsupported signal strategy: {strategy}")
        impl = self._strategies[strategy]
        affinity_map = self.strategy_affinity_map(strategy)
        htf_policy = getattr(impl, "htf_policy", None)
        return {
            "name": strategy,
            "category": self.strategy_category(strategy) or None,
            "htf_policy": htf_policy.value if hasattr(htf_policy, "value") else None,
            "preferred_scopes": list(self.strategy_scopes(strategy)),
            "required_indicators": list(self.strategy_requirements(strategy)),
            "regime_affinity": {
                (key.value if hasattr(key, "value") else str(key)): float(value)
                for key, value in affinity_map.items()
            },
        }

    def strategy_catalog(self) -> list[dict[str, Any]]:
        return [self.describe_strategy(name) for name in self.list_strategies()]

    def all_required_indicators(self) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for strategy_name in self.list_strategies():
            for indicator_name in self.strategy_requirements(strategy_name):
                if indicator_name in seen:
                    continue
                seen.add(indicator_name)
                ordered.append(indicator_name)
        return ordered

    def required_indicator_groups(self) -> list[tuple[str, ...]]:
        ordered: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for strategy_name in self.list_strategies():
            group = tuple(self.strategy_requirements(strategy_name))
            if not group or group in seen:
                continue
            seen.add(group)
            ordered.append(group)
        return ordered

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        strategy: str,
        indicators: Optional[Dict[str, Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        persist: bool = True,
        htf_indicators: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    ) -> SignalDecision:
        strategy_impl = self._strategies.get(strategy)
        if strategy_impl is None:
            raise ValueError(f"unsupported signal strategy: {strategy}")

        indicator_payload = indicators or self.indicator_source.get_all_indicators(
            symbol, timeframe
        )
        bars_depth = getattr(strategy_impl, "recent_bars_depth", 5)
        context_metadata = self._build_context_metadata(
            symbol,
            timeframe,
            metadata,
            recent_bars_limit=bars_depth,
        )
        event_impact = context_metadata.pop("_event_impact_forecast", None)
        context = SignalContext(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            indicators=indicator_payload,
            metadata=context_metadata,
            htf_indicators=htf_indicators or {},
            event_impact_forecast=event_impact,
        )
        decision = strategy_impl.evaluate(context)

        # ── 原始置信度门槛（提前拦截低质量信号，省后续计算）────────────
        min_raw = context_metadata.get(MK.MIN_RAW_CONFIDENCE, 0.45)
        if decision.direction in ("buy", "sell") and decision.confidence < min_raw:
            return decision  # 策略本身不够确信，跳过 affinity/perf/calibrator

        # ── Regime 亲和度修正 ────────────────────────────────────────────
        # 从策略类属性 regime_affinity 读取当前 Regime 对应的乘数，
        # 压制在当前行情类型下不可靠的策略。
        # 置信度降低后若低于 min_preview_confidence（默认 0.55），
        # SignalRuntime 状态机会自然忽略该信号，无需在此处做额外的硬截断。
        #
        # 性能优化：runtime 在 process_next_event 循环开始前会检测一次 Regime，
        # 并将结果写入 metadata["_regime"]。若存在则直接复用，跳过重复检测。
        soft_regime = self._resolve_soft_regime(indicator_payload, context_metadata)
        if soft_regime is not None:
            regime = soft_regime.dominant_regime
        else:
            pre_computed = context_metadata.get(MK.REGIME_HARD)
            if pre_computed:
                try:
                    regime = RegimeType(pre_computed)
                except ValueError:
                    regime = self._regime_detector.detect(indicator_payload)
            else:
                regime = self._regime_detector.detect(indicator_payload)
        # 复用 runtime 预计算的 affinity（避免重复计算和 getattr 开销）。
        pre_computed_affinity = context_metadata.get(MK.PRE_COMPUTED_AFFINITY)
        if pre_computed_affinity is not None:
            affinity = max(0.0, min(float(pre_computed_affinity), 1.0))
        else:
            # 回退路径：从策略注册表缓存中读取 affinity_map（无 getattr 开销）
            affinity_map = self._strategy_affinity_cache.get(strategy)
            if affinity_map is None:
                affinity_map = getattr(strategy_impl, "regime_affinity", {})
                self._strategy_affinity_cache[strategy] = affinity_map
            affinity = self._effective_affinity(affinity_map, regime, soft_regime)
        post_affinity = decision.confidence * affinity
        # ── 日内绩效乘数（实时反馈层）────────────────────────────────
        # 在 Regime 亲和度之后、长期校准器之前施加日内绩效乘数。
        # 基于当前 session 的策略表现（win_rate、streak、profit_factor），
        # 对表现差的策略压制置信度，对表现好的微幅提升。
        # performance_tracker=None（默认）时此步跳过。
        perf_multiplier = 1.0
        if self._performance_tracker is not None and decision.direction in (
            "buy",
            "sell",
        ):
            perf_multiplier = self._performance_tracker.get_multiplier(
                decision.strategy,
                regime=regime.value,
            )
        post_performance = post_affinity * perf_multiplier
        # ── 置信度校准（历史胜率反馈层）────────────────────────────────
        # 在 Regime 亲和度修正之后，再根据该策略 (action, regime) 的历史胜率
        # 做混合校准，使置信度逐步收敛到与实际盈亏挂钩的值。
        # calibrator=None（默认）时此步跳过，对现有行为零影响。
        if self._calibrator is not None and decision.direction in ("buy", "sell"):
            calibrated = self._calibrator.calibrate(
                strategy=decision.strategy,
                action=decision.direction,
                raw_confidence=post_performance,
                regime=regime,
                timeframe=timeframe,
            )
        else:
            calibrated = post_performance

        # ── 多层压制底线保护 ─────────────────────────────────────────────
        # regime_affinity × session_performance × calibrator 的乘法链可能导致
        # 置信度被过度压制（如 0.80 × 0.70 × 0.60 × 0.90 = 0.30）。
        # 设置底线，确保原始置信度较高的信号不会被完全淹没。
        # 底线仅在原始 confidence > 0 时生效（hold 信号不受影响）。
        # affinity 低于阈值表示 regime 强烈反对，不应通过 floor 挽救。
        if (
            decision.confidence > 0
            and calibrated < self._confidence_floor
            and affinity >= self._confidence_floor_min_affinity
        ):
            calibrated = self._confidence_floor

        # ── 置信度管线摘要日志 ──────────────────────────────────────────
        if decision.direction in ("buy", "sell") and decision.confidence > 0:
            delta = calibrated - decision.confidence
            if abs(delta) > 0.05:
                logger.info(
                    "Confidence[%s/%s/%s %s]: %.2f → %.2f "
                    "(affinity=%.2f perf=%.2f regime=%s)",
                    symbol,
                    timeframe,
                    strategy,
                    decision.direction,
                    decision.confidence,
                    calibrated,
                    affinity,
                    perf_multiplier,
                    regime.value,
                )

        # 追加下游修正步骤到置信度审计链
        trace = list(decision.confidence_trace)
        if affinity != 1.0:
            trace.append(("regime_affinity", round(post_affinity, 4)))
        if perf_multiplier != 1.0:
            trace.append(("performance", round(post_performance, 4)))
        if calibrated != post_performance:
            trace.append(("calibrator", round(calibrated, 4)))

        decision = dataclasses.replace(
            decision,
            confidence=calibrated,
            confidence_trace=trace,
            metadata={
                **decision.metadata,
                "category": getattr(strategy_impl, "category", ""),
                "regime": regime.value,
                "regime_affinity": affinity,
                "regime_source": "soft" if soft_regime is not None else "hard",
                "regime_probabilities": (
                    {item.value: soft_regime.probability(item) for item in RegimeType}
                    if soft_regime is not None
                    else {regime.value: 1.0}
                ),
                "dominant_regime_probability": (
                    soft_regime.probability(regime) if soft_regime is not None else 1.0
                ),
                "raw_confidence": decision.confidence,  # 原始规则输出
                "post_affinity_confidence": post_affinity,  # 仅 affinity
                "post_performance_confidence": post_performance,  # affinity + perf
                "session_performance_multiplier": perf_multiplier,
                "calibrated": self._calibrator is not None
                and decision.direction in ("buy", "sell"),
            },
        )

        if persist:
            self._persist_signal(decision, indicator_payload, context_metadata)
        return decision

    def _build_context_metadata(
        self,
        symbol: str,
        timeframe: str,
        metadata: Optional[Dict[str, Any]],
        recent_bars_limit: int = 5,
    ) -> Dict[str, Any]:
        context_metadata = dict(metadata or {})
        if MK.RECENT_BARS in context_metadata:
            return context_metadata

        getter = getattr(self.indicator_source, "get_recent_bars", None)
        if not callable(getter):
            return context_metadata

        end_time = self._parse_optional_datetime(context_metadata.get(MK.BAR_TIME))
        try:
            recent_bars = getter(
                symbol, timeframe, end_time=end_time, limit=recent_bars_limit
            )
        except Exception:
            log_fn = logger.warning if recent_bars_limit > 5 else logger.debug
            log_fn(
                "Failed to load recent bars for %s/%s (limit=%d)",
                symbol,
                timeframe,
                recent_bars_limit,
                exc_info=True,
            )
            return context_metadata

        if recent_bars:
            context_metadata[MK.RECENT_BARS] = [
                dataclasses.asdict(b) if dataclasses.is_dataclass(b) else b
                for b in recent_bars
            ]
        return context_metadata

    def _resolve_soft_regime(
        self,
        indicators: Dict[str, Dict[str, Any]],
        metadata: Dict[str, Any],
    ) -> Optional[SoftRegimeResult]:
        # 优先复用 runtime 预计算的 soft regime（避免重复调用 detect_soft）
        precomputed = metadata.get(MK.REGIME_SOFT)
        if isinstance(precomputed, SoftRegimeResult):
            return precomputed
        if isinstance(precomputed, dict):
            try:
                return SoftRegimeResult.from_dict(precomputed)
            except Exception:
                logger.debug("Invalid precomputed soft regime payload", exc_info=True)
        if not self._soft_regime_enabled:
            return None
        # Standalone 模式（无 runtime 预计算）才自行检测
        if "_regime" not in metadata:
            try:
                return self._regime_detector.detect_soft(indicators)
            except Exception:
                logger.debug("Failed to compute soft regime", exc_info=True)
        return None

    @staticmethod
    def _effective_affinity(
        affinity_map: Dict[RegimeType, float],
        regime: Optional[RegimeType],
        soft_regime: Optional[SoftRegimeResult | Dict[str, Any]],
    ) -> float:
        if isinstance(soft_regime, dict):
            try:
                soft_regime = SoftRegimeResult.from_dict(soft_regime)
            except Exception:
                soft_regime = None
        if isinstance(soft_regime, SoftRegimeResult):
            weighted = sum(
                soft_regime.probability(item) * affinity_map.get(item, 0.5)
                for item in RegimeType
            )
            return weighted
        if regime is None:
            return 0.5
        return affinity_map.get(regime, 0.5)

    @staticmethod
    def _parse_optional_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    def _persist_signal(
        self,
        decision: SignalDecision,
        indicators: Dict[str, Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[SignalRecord]:
        if self.repository is None:
            return None
        try:
            merged_metadata = dict(decision.metadata)
            if metadata:
                merged_metadata.update(metadata)
            record = SignalRecord.from_decision(
                decision,
                indicators_snapshot=indicators,
                metadata=merged_metadata,
            )
            self.repository.append(record)
            return record
        except Exception:
            logger.exception(
                "Failed to persist signal event for %s/%s",
                decision.symbol,
                decision.strategy,
            )
            return None

    def persist_decision(
        self,
        decision: SignalDecision,
        indicators: Dict[str, Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[SignalRecord]:
        return self._persist_signal(decision, indicators, metadata)

    def recent_signals(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return self.repository.recent(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            scope=scope,
            limit=limit,
        )

    def summary(
        self, *, hours: int = 24, scope: str = "confirmed"
    ) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return self.repository.summary(hours=hours, scope=scope)

    def strategy_diagnostics(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 2000,
        conflict_warn_threshold: float = 0.35,
        hold_warn_threshold: float = 0.75,
        confidence_warn_threshold: float = 0.45,
    ) -> dict[str, Any]:
        """聚合最近信号，输出策略冲突与质量诊断。

        该诊断用于排查「策略之间互相打架」及「指标缺失导致频繁 hold」问题，
        为后续工程化治理（下线策略、调参、补指标）提供量化依据。
        """
        rows = self.recent_signals(
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            limit=limit,
        )
        thresholds = DiagnosticThresholds(
            conflict_warn_threshold=conflict_warn_threshold,
            hold_warn_threshold=hold_warn_threshold,
            confidence_warn_threshold=confidence_warn_threshold,
        )
        report = self._diagnostics_analyzer.build_report(
            rows,
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            thresholds=thresholds,
        )
        return self._attach_expectancy_profile(report, symbol=symbol)

    def daily_quality_report(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 5000,
        conflict_warn_threshold: float = 0.35,
        hold_warn_threshold: float = 0.75,
        confidence_warn_threshold: float = 0.45,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        rows = self.recent_signals(
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            limit=limit,
        )
        thresholds = DiagnosticThresholds(
            conflict_warn_threshold=conflict_warn_threshold,
            hold_warn_threshold=hold_warn_threshold,
            confidence_warn_threshold=confidence_warn_threshold,
        )
        report = self._diagnostics_analyzer.build_daily_quality_report(
            rows,
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            thresholds=thresholds,
            now=now,
        )
        return self._attach_expectancy_profile(report, symbol=symbol)

    def diagnostics_aggregate_summary(
        self,
        *,
        hours: int = 24,
        scope: str = "confirmed",
    ) -> dict[str, Any]:
        rows = self.summary(hours=hours, scope=scope)
        direction_totals: dict[str, int] = {}
        strategy_totals: dict[str, int] = {}
        for row in rows:
            direction = str(row.get("direction") or "unknown")
            strategy = str(row.get("strategy") or "unknown")
            count = int(row.get("count") or 0)
            direction_totals[direction] = direction_totals.get(direction, 0) + count
            strategy_totals[strategy] = strategy_totals.get(strategy, 0) + count
        top_strategies = sorted(
            (
                {"strategy": strategy, "count": count}
                for strategy, count in strategy_totals.items()
            ),
            key=lambda item: item["count"],
            reverse=True,
        )[:10]
        return {
            "hours": hours,
            "scope": scope,
            "rows_analyzed": len(rows),
            "direction_totals": direction_totals,
            "strategy_totals": strategy_totals,
            "top_strategies": top_strategies,
            "source": "repository.summary",
        }

    def regime_report(
        self,
        *,
        symbol: str,
        timeframe: str,
        runtime: Optional[Any] = None,
    ) -> dict[str, Any]:
        indicators = self.indicator_source.get_all_indicators(symbol, timeframe)
        detail = self._regime_detector.detect_with_detail(indicators)
        stability = None
        if runtime is not None and hasattr(runtime, "get_regime_stability"):
            stability = runtime.get_regime_stability(symbol, timeframe)
        return {
            **detail,
            "symbol": symbol,
            "timeframe": timeframe,
            "stability": stability,
        }

    def recent_consensus_signals(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.recent_signals(
            symbol=symbol,
            timeframe=timeframe,
            strategy="consensus",
            scope="confirmed",
            limit=limit,
        )

    def strategy_winrates(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if self.repository is None or not hasattr(self.repository, "fetch_winrates"):
            return []
        return self.repository.fetch_winrates(hours=hours, symbol=symbol)

    def strategy_expectancy(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if self.repository is None or not hasattr(
            self.repository, "fetch_expectancy_stats"
        ):
            return []
        return self.repository.fetch_expectancy_stats(hours=hours, symbol=symbol)

    def _attach_expectancy_profile(
        self,
        report: dict[str, Any],
        *,
        symbol: Optional[str],
    ) -> dict[str, Any]:
        expectancy_rows = self.strategy_expectancy(symbol=symbol)
        if not expectancy_rows:
            report.setdefault("performance_profile", [])
            return report

        expectancy_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for row in expectancy_rows:
            expectancy_by_strategy.setdefault(
                str(row.get("strategy") or "unknown"), []
            ).append(row)

        strategy_breakdown = report.get("strategy_breakdown")
        if isinstance(strategy_breakdown, list):
            for item in strategy_breakdown:
                strategy_name = str(item.get("strategy") or "unknown")
                candidates = expectancy_by_strategy.get(strategy_name, [])
                if not candidates:
                    item["expectancy"] = None
                    item["payoff_ratio"] = None
                    continue
                best = max(
                    candidates,
                    key=lambda row: abs(float(row.get("expectancy") or 0.0)),
                )
                item["expectancy"] = best.get("expectancy")
                item["payoff_ratio"] = best.get("payoff_ratio")

        negative_rows = [
            row for row in expectancy_rows if float(row.get("expectancy") or 0.0) < 0.0
        ]
        recommendations = report.setdefault("recommendations", [])
        if negative_rows:
            recommendations.append(
                "negative_expectancy_detected: cut or retune strategies with negative net expectancy before expanding auto-trade"
            )
        report["performance_profile"] = expectancy_rows
        return report

    def session_performance(self) -> dict[str, Any]:
        """返回日内策略绩效追踪器的状态快照。"""
        if self._performance_tracker is None:
            return {"enabled": False}
        return self._performance_tracker.describe()

    def session_performance_ranking(self) -> list[dict[str, Any]]:
        """返回按绩效乘数排序的策略排名。"""
        if self._performance_tracker is None:
            return []
        return self._performance_tracker.strategy_ranking()

    def recent_by_trace_id(
        self,
        *,
        trace_id: str,
        scope: str = "all",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        rows = self.recent_signals(scope=scope, limit=limit)
        return [
            row
            for row in rows
            if str((row.get("metadata") or {}).get("signal_trace_id", "")).strip()
            == trace_id
        ]

    def dispatch_operation(
        self, operation: str, payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        payload = payload or {}
        handlers = {
            "evaluate": lambda: self.evaluate(
                symbol=payload["symbol"],
                timeframe=payload["timeframe"],
                strategy=payload["strategy"],
                indicators=payload.get("indicators"),
                metadata=payload.get("metadata"),
                persist=payload.get("persist", True),
            ).to_dict(),
            "strategies": self.list_strategies,
            "strategy_requirements": lambda: {
                name: list(self.strategy_requirements(name))
                for name in self.list_strategies()
            },
            "required_indicator_groups": lambda: [
                list(group) for group in self.required_indicator_groups()
            ],
            "available_indicators": lambda: [
                item.get("name") for item in self.indicator_source.list_indicators()
            ],
            "recent": lambda: self.recent_signals(
                symbol=payload.get("symbol"),
                timeframe=payload.get("timeframe"),
                strategy=payload.get("strategy"),
                direction=payload.get("direction"),
                scope=payload.get("scope", "confirmed"),
                limit=payload.get("limit", 200),
            ),
            "summary": lambda: self.summary(
                hours=payload.get("hours", 24), scope=payload.get("scope", "confirmed")
            ),
            "strategy_diagnostics": lambda: self.strategy_diagnostics(
                symbol=payload.get("symbol"),
                timeframe=payload.get("timeframe"),
                scope=payload.get("scope", "confirmed"),
                limit=payload.get("limit", 2000),
                conflict_warn_threshold=payload.get("conflict_warn_threshold", 0.35),
                hold_warn_threshold=payload.get("hold_warn_threshold", 0.75),
                confidence_warn_threshold=payload.get(
                    "confidence_warn_threshold", 0.45
                ),
            ),
            "daily_quality_report": lambda: self.daily_quality_report(
                symbol=payload.get("symbol"),
                timeframe=payload.get("timeframe"),
                scope=payload.get("scope", "confirmed"),
                limit=payload.get("limit", 5000),
                conflict_warn_threshold=payload.get("conflict_warn_threshold", 0.35),
                hold_warn_threshold=payload.get("hold_warn_threshold", 0.75),
                confidence_warn_threshold=payload.get(
                    "confidence_warn_threshold", 0.45
                ),
            ),
            "diagnostics_aggregate_summary": lambda: self.diagnostics_aggregate_summary(
                hours=payload.get("hours", 24),
                scope=payload.get("scope", "confirmed"),
            ),
            "regime_report": lambda: self.regime_report(
                symbol=payload["symbol"],
                timeframe=payload["timeframe"],
                runtime=payload.get("runtime"),
            ),
            "recent_consensus": lambda: self.recent_consensus_signals(
                symbol=payload.get("symbol"),
                timeframe=payload.get("timeframe"),
                limit=payload.get("limit", 50),
            ),
            "strategy_winrates": lambda: self.strategy_winrates(
                hours=payload.get("hours", 168),
                symbol=payload.get("symbol"),
            ),
            "strategy_expectancy": lambda: self.strategy_expectancy(
                hours=payload.get("hours", 168),
                symbol=payload.get("symbol"),
            ),
            "session_performance": self.session_performance,
            "session_performance_ranking": self.session_performance_ranking,
            "recent_by_trace_id": lambda: self.recent_by_trace_id(
                trace_id=payload["trace_id"],
                scope=payload.get("scope", "all"),
                limit=payload.get("limit", 2000),
            ),
        }
        if operation not in handlers:
            raise ValueError(f"unsupported signal operation: {operation}")
        return handlers[operation]()
