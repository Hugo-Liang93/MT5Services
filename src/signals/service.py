from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from . import service_diagnostics as _svc_diag
from .analytics import (
    DiagnosticsEngine,
    DiagnosticThresholds,
    SignalDiagnosticsAnalyzer,
)
from .contracts import StrategyCapability
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
        # P10.2 修复：account_bindings 反向索引（strategy → [account_alias]）。
        # 由 factory 在构建完成后通过 set_account_bindings() 注入；空 dict = 未配置。
        self._strategy_to_accounts: dict[str, list[str]] = {}
        default_strategies: Iterable[SignalStrategy] = (
            list(strategies) if strategies is not None else build_default_strategy_set()
        )
        for strategy in default_strategies:
            self.register_strategy(strategy)

    def set_account_bindings(
        self, bindings: Optional[dict[str, Iterable[str]]]
    ) -> None:
        """P10.2: 注入 {account_alias: [strategy]} 配置并建立反向索引。

        Intel / Cockpit 等跨账户读模型通过 strategy_account_bindings() 访问，
        不再各自读 signal_config。
        """
        index: dict[str, list[str]] = {}
        for account_alias, strategy_list in (bindings or {}).items():
            alias = str(account_alias or "").strip()
            if not alias or not strategy_list:
                continue
            for strategy_name in strategy_list:
                name = str(strategy_name or "").strip()
                if not name:
                    continue
                index.setdefault(name, []).append(alias)
        self._strategy_to_accounts = index

    def strategy_account_bindings(self) -> dict[str, list[str]]:
        """P10.2: 返回 strategy → [account_alias] 反向索引（防御性拷贝）。"""
        return {k: list(v) for k, v in self._strategy_to_accounts.items()}

    @property
    def regime_detector(self) -> MarketRegimeDetector:
        """Return the active regime detector as an explicit module interface."""
        return self._regime_detector

    @property
    def performance_tracker(self) -> Optional[StrategyPerformanceTracker]:
        """Return the optional performance tracker as an explicit module interface."""
        return self._performance_tracker

    @property
    def diagnostics_engine(self) -> DiagnosticsEngine:
        """Return the diagnostics engine as an explicit module interface."""
        return self._diagnostics_analyzer

    @property
    def confidence_floor(self) -> float:
        """Return configured confidence floor used by runtime confidence hardening."""
        return self._confidence_floor

    @property
    def confidence_floor_min_affinity(self) -> float:
        """Return the affinity threshold below which confidence floor will not be applied."""
        return self._confidence_floor_min_affinity

    def reset_performance_session(self) -> None:
        """Rotate performance tracker session counters if tracking is enabled."""
        if self._performance_tracker is not None:
            self._performance_tracker.check_session_reset()

    def has_performance_tracker(self) -> bool:
        """Return whether strategy performance tracker is active."""
        return self._performance_tracker is not None

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
        """按能力快照计算 intrabar 必需指标集合。"""
        result: set[str] = set()
        for capability in self.strategy_capability_catalog():
            if not capability.needs_intrabar:
                continue
            for ind in capability.needed_indicators:
                result.add(str(ind))
        return frozenset(result)

    def confirmed_required_indicators(self) -> frozenset:
        """按能力快照计算 confirmed 运行所需指标并集。"""
        result: set[str] = set()
        for capability in self.strategy_capability_catalog():
            for ind in capability.needed_indicators:
                result.add(str(ind))
        return frozenset(result)

    def htf_required_indicators(self) -> frozenset:
        """按能力快照推导 HTF 跨 TF 所需指标。"""
        result: set[str] = set()
        for capability in self.strategy_capability_catalog():
            result.update(str(name) for name in capability.htf_requirements.keys())
        return frozenset(result)

    def htf_target_config(self) -> Dict[str, str]:
        """从能力快照自动推导 HTF 指标注入配置。"""
        config: Dict[str, str] = {}
        for capability in self.strategy_capability_catalog():
            if not capability.htf_requirements:
                continue
            for ind, tf in capability.htf_requirements.items():
                config[f"{capability.name}.{ind}"] = str(tf)
        return config

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
        self.set_tf_param_resolver(resolver)

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

    @property
    def strategies(self) -> dict[str, "SignalStrategy"]:
        """只读访问已注册策略字典（供装配层使用）。"""
        return self._strategies

    def set_tf_param_resolver(self, resolver: Any) -> None:
        """注入 per-TF 参数查表器到模块及所有已注册策略。"""
        self._tf_param_resolver = resolver  # type: ignore[attr-defined]
        for strategy in self._strategies.values():
            strategy.tf_param_resolver = resolver

    def get_strategy(self, name: str) -> Optional["SignalStrategy"]:
        """Return strategy instance by name, or None if not registered."""
        return self._strategies.get(name)

    def list_strategies(self) -> list[str]:
        return sorted(self._strategies.keys())

    def strategy_capability_index(self) -> tuple[StrategyCapability, ...]:
        """返回所有已注册策略的能力快照（只读，供运行时/回测共享）。

        返回值是可持久化/可序列化的能力摘要，字段含义：
        - needs_intrabar：是否声明了 "intrabar" scope
        - needed_indicators：评估所需指标名（有序）
        - valid_scopes：支持的 scope 列表
        - regime_affinity：按 regime 名称序列化后的 affinity 映射
        - htf_requirements：HTF 指标需求映射
        """
        capabilities: list[StrategyCapability] = []
        for name in self.list_strategies():
            impl = self._strategies[name]
            affinity_map = self.strategy_affinity_map(name) or {}
            needed_indicators = tuple(str(item) for item in impl.required_indicators)
            valid_scopes = tuple(str(scope) for scope in impl.preferred_scopes)
            raw_htf_requirements = getattr(impl, "htf_required_indicators", {})
            if isinstance(raw_htf_requirements, dict):
                htf_requirements = {
                    str(k): str(v) for k, v in raw_htf_requirements.items()
                }
            else:
                htf_requirements = {}
            affinity_payload = {
                (key.value if hasattr(key, "value") else str(key)): float(
                    affinity_map[key]
                )
                for key in affinity_map
            }
            capabilities.append(
                StrategyCapability(
                    name=name,
                    valid_scopes=valid_scopes,
                    needed_indicators=needed_indicators,
                    needs_intrabar=("intrabar" in valid_scopes),
                    needs_htf=bool(htf_requirements),
                    regime_affinity=affinity_payload,
                    htf_requirements=htf_requirements,
                )
            )
        return tuple(capabilities)

    def strategy_capability_contract(self) -> tuple[dict[str, Any], ...]:
        """标准化能力清单字典口，module 与 policy 对账的统一视图。"""
        return tuple(
            capability.as_contract()
            for capability in self.strategy_capability_catalog()
        )

    def strategy_capability_catalog(self) -> tuple[StrategyCapability, ...]:
        """标准化能力快照口，供运行时与回测共享同一输入契约。"""
        return self.strategy_capability_index()

    def strategy_capability_matrix(self) -> list[dict[str, Any]]:
        """返回策略能力能力快照的只读摘要，用于调度与可观测链路共享。

        返回字段为策略能力规范的最小公共形态：
        - name: 策略名称
        - valid_scopes: 支持的 scope 列表
        - needed_indicators: 齐套判定指标
        - needs_intrabar: 是否需要 intrabar scope
        - needs_htf: 是否声明 HTF 需求
        """
        return list(self.strategy_capability_contract())

    def list_intrabar_strategies(self) -> list[str]:
        """返回 needs_intrabar 的策略名列表。"""
        return sorted(
            capability.name
            for capability in self.strategy_capability_catalog()
            if capability.needs_intrabar
        )

    def strategy_capability(self, strategy: str) -> StrategyCapability:
        normalized = str(strategy)
        for capability in self.strategy_capability_catalog():
            if capability.name == normalized:
                return capability
        raise ValueError(f"unsupported signal strategy: {strategy}")

    def strategy_requirements(self, strategy: str) -> tuple[str, ...]:
        return self.strategy_capability(strategy).needed_indicators

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

        Reads the declared strategy capability.
        """
        return self.strategy_capability(strategy).valid_scopes

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
        # 策略实现只负责方向/置信度/原因，不应污染事件身份字段。
        # 在信号编排边界统一收口 symbol/timeframe/strategy 契约，
        # 避免 recent/summary/persistence 被空串或错配字段污染。
        decision = dataclasses.replace(
            decision,
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
        )

        # ── 原始置信度门槛（提前拦截低质量信号，省后续计算）────────────
        min_raw = context_metadata.get(MK.MIN_RAW_CONFIDENCE, 0.45)
        if decision.direction in ("buy", "sell") and decision.confidence < min_raw:
            return decision  # 策略本身不够确信，跳过 affinity/perf/calibrator

        # ── Regime 亲和度修正 ────────────────────────────────────────────
        # 从策略类属性 regime_affinity 读取当前 Regime 对应的乘数，
        # 压制在当前行情类型下不可靠的策略。
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
            # 默认路径：从策略注册表缓存中读取 affinity_map（无 getattr 开销）
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

    def recent_signal_page(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        actionability: Optional[str] = None,
        scope: str = "confirmed",
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 200,
        sort: str = "generated_at_desc",
    ) -> dict[str, Any]:
        if self.repository is None:
            return {
                "items": [],
                "total": 0,
                "page": max(1, int(page)),
                "page_size": max(1, int(page_size)),
            }

        pager = getattr(self.repository, "recent_page", None)
        if callable(pager):
            return pager(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                direction=direction,
                status=status,
                actionability=actionability,
                scope=scope,
                from_time=from_time,
                to_time=to_time,
                page=page,
                page_size=page_size,
                sort=sort,
            )

        effective_page = max(1, int(page))
        effective_page_size = max(1, int(page_size))
        rows = self.recent_signals(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            scope=scope,
            limit=effective_page * effective_page_size,
        )
        start = (effective_page - 1) * effective_page_size
        end = start + effective_page_size
        return {
            "items": rows[start:end],
            "total": len(rows),
            "page": effective_page,
            "page_size": effective_page_size,
        }

    def summary(
        self, *, hours: int = 24, scope: str = "confirmed"
    ) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return self.repository.summary(hours=hours, scope=scope)

    def strategy_diagnostics(self, **kwargs: Any) -> dict[str, Any]:
        return _svc_diag.strategy_diagnostics(self, **kwargs)

    def daily_quality_report(self, **kwargs: Any) -> dict[str, Any]:
        return _svc_diag.daily_quality_report(self, **kwargs)

    def diagnostics_aggregate_summary(self, **kwargs: Any) -> dict[str, Any]:
        return _svc_diag.diagnostics_aggregate_summary(self, **kwargs)

    def regime_report(self, **kwargs: Any) -> dict[str, Any]:
        return _svc_diag.regime_report(self, **kwargs)

    def strategy_winrates(self, **kwargs: Any) -> list[dict[str, Any]]:
        return _svc_diag.strategy_winrates(self, **kwargs)

    def strategy_expectancy(self, **kwargs: Any) -> list[dict[str, Any]]:
        return _svc_diag.strategy_expectancy(self, **kwargs)

    def strategy_audit(self, **kwargs: Any) -> dict[str, Any]:
        return _svc_diag.strategy_audit(self, **kwargs)

    def session_performance(self) -> dict[str, Any]:
        return _svc_diag.session_performance(self)

    def session_performance_ranking(self) -> list[dict[str, Any]]:
        return _svc_diag.session_performance_ranking(self)

    def recent_by_trace_id(self, **kwargs: Any) -> list[dict[str, Any]]:
        return _svc_diag.recent_by_trace_id(self, **kwargs)

    def dispatch_operation(
        self, operation: str, payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        return _svc_diag.dispatch_operation(self, operation, payload)
