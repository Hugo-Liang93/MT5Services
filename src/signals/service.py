from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from .adapters import IndicatorSource
from .analytics import (
    DiagnosticThresholds,
    DiagnosticsEngine,
    SignalDiagnosticsAnalyzer,
)
from .calibrator import ConfidenceCalibrator
from .models import SignalContext, SignalDecision, SignalRecord
from .regime import MarketRegimeDetector, RegimeType
from .repository import SignalRepository
from .strategies import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    EmaRibbonStrategy,
    KeltnerBollingerSqueezeStrategy,
    MacdMomentumStrategy,
    MultiTimeframeConfirmStrategy,
    RsiReversionStrategy,
    SignalStrategy,
    SmaTrendStrategy,
    StochRsiStrategy,
    SupertrendStrategy,
)

logger = logging.getLogger(__name__)


class SignalModule:
    def __init__(
        self,
        indicator_source: IndicatorSource,
        strategies: Optional[Iterable[SignalStrategy]] = None,
        repository: Optional[SignalRepository] = None,
        regime_detector: Optional[MarketRegimeDetector] = None,
        calibrator: Optional[ConfidenceCalibrator] = None,
        diagnostics_engine: Optional[DiagnosticsEngine] = None,
    ):
        self.indicator_source = indicator_source
        self.repository = repository
        self._regime_detector: MarketRegimeDetector = (
            regime_detector or MarketRegimeDetector()
        )
        # 置信度校准器：基于历史胜率对原始 confidence 进行混合校准。
        # None = 不校准（新部署或没有足够历史数据时的默认状态）。
        self._calibrator: Optional[ConfidenceCalibrator] = calibrator
        self._diagnostics_analyzer: DiagnosticsEngine = (
            diagnostics_engine or SignalDiagnosticsAnalyzer()
        )
        self._strategies: dict[str, SignalStrategy] = {}
        default_strategies: Iterable[SignalStrategy] = strategies or (
            SmaTrendStrategy(),
            RsiReversionStrategy(),
            BollingerBreakoutStrategy(),
            # MultiTimeframeConfirmStrategy 需要外部注入 state_reader 和 htf_key
            # 才能发挥作用；在默认配置下 htf_key 永不填充，策略始终输出 hold。
            # 如需使用，请手动构造并传入 strategies 参数：
            #   MultiTimeframeConfirmStrategy(state_reader=runtime._state_by_target)
            SupertrendStrategy(),
            StochRsiStrategy(),
            MacdMomentumStrategy(),
            KeltnerBollingerSqueezeStrategy(),
            DonchianBreakoutStrategy(),
            EmaRibbonStrategy(),
        )
        for strategy in default_strategies:
            self.register_strategy(strategy)

    def register_strategy(self, strategy: SignalStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def list_strategies(self) -> list[str]:
        return sorted(self._strategies.keys())

    def strategy_requirements(self, strategy: str) -> tuple[str, ...]:
        strategy_impl = self._strategies.get(strategy)
        if strategy_impl is None:
            raise ValueError(f"unsupported signal strategy: {strategy}")
        requirements = getattr(strategy_impl, "required_indicators", ())
        return tuple(str(item) for item in requirements)

    def strategy_affinity_map(self, strategy: str) -> Dict[RegimeType, float]:
        """返回策略的 regime_affinity 字典（不存在时返回空字典）。

        运行时在启动时缓存此结果，避免每次 process_next_event 重复 getattr。
        """
        impl = self._strategies.get(strategy)
        return getattr(impl, "regime_affinity", {}) if impl else {}

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
    ) -> SignalDecision:
        strategy_impl = self._strategies.get(strategy)
        if strategy_impl is None:
            raise ValueError(f"unsupported signal strategy: {strategy}")

        indicator_payload = indicators or self.indicator_source.get_all_indicators(
            symbol, timeframe
        )
        context = SignalContext(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            indicators=indicator_payload,
            metadata=metadata or {},
        )
        decision = strategy_impl.evaluate(context)

        # ── Regime 亲和度修正 ────────────────────────────────────────────
        # 从策略类属性 regime_affinity 读取当前 Regime 对应的乘数，
        # 压制在当前行情类型下不可靠的策略。
        # 置信度降低后若低于 min_preview_confidence（默认 0.55），
        # SignalRuntime 状态机会自然忽略该信号，无需在此处做额外的硬截断。
        #
        # 性能优化：runtime 在 process_next_event 循环开始前会检测一次 Regime，
        # 并将结果写入 metadata["_regime"]。若存在则直接复用，跳过重复检测。
        pre_computed = (metadata or {}).get("_regime")
        if pre_computed:
            try:
                regime = RegimeType(pre_computed)
            except ValueError:
                regime = self._regime_detector.detect(indicator_payload)
        else:
            regime = self._regime_detector.detect(indicator_payload)
        # 优先读策略类上的 regime_affinity 属性；缺失时回退到中性值 0.5
        affinity_map: Dict[RegimeType, float] = getattr(
            strategy_impl, "regime_affinity", {}
        )
        affinity = affinity_map.get(regime, 0.5)
        adjusted_confidence = decision.confidence * affinity
        # ── 置信度校准（历史胜率反馈层）────────────────────────────────
        # 在 Regime 亲和度修正之后，再根据该策略 (action, regime) 的历史胜率
        # 做混合校准，使置信度逐步收敛到与实际盈亏挂钩的值。
        # calibrator=None（默认）时此步跳过，对现有行为零影响。
        raw_post_affinity = adjusted_confidence
        if self._calibrator is not None and decision.action in ("buy", "sell"):
            calibrated = self._calibrator.calibrate(
                strategy=decision.strategy,
                action=decision.action,
                raw_confidence=raw_post_affinity,
                regime=regime,
            )
        else:
            calibrated = raw_post_affinity

        decision = dataclasses.replace(
            decision,
            confidence=calibrated,
            metadata={
                **decision.metadata,
                "regime": regime.value,
                "regime_affinity": affinity,
                "raw_confidence": decision.confidence,  # 原始规则输出
                "post_affinity_confidence": raw_post_affinity,
                "calibrated": self._calibrator is not None
                and decision.action in ("buy", "sell"),
            },
        )

        if persist:
            self._persist_signal(decision, indicator_payload, metadata)
        return decision

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
        action: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return self.repository.recent(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            action=action,
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
        return self._diagnostics_analyzer.build_report(
            rows,
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            thresholds=thresholds,
        )

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
        return self._diagnostics_analyzer.build_daily_quality_report(
            rows,
            symbol=symbol,
            timeframe=timeframe,
            scope=scope,
            thresholds=thresholds,
            now=now,
        )

    def diagnostics_aggregate_summary(
        self,
        *,
        hours: int = 24,
        scope: str = "confirmed",
    ) -> dict[str, Any]:
        rows = self.summary(hours=hours, scope=scope)
        action_totals: dict[str, int] = {}
        strategy_totals: dict[str, int] = {}
        for row in rows:
            action = str(row.get("action") or "unknown")
            strategy = str(row.get("strategy") or "unknown")
            count = int(row.get("count") or 0)
            action_totals[action] = action_totals.get(action, 0) + count
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
            "action_totals": action_totals,
            "strategy_totals": strategy_totals,
            "top_strategies": top_strategies,
            "source": "repository.summary",
        }

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
                action=payload.get("action"),
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
            "recent_by_trace_id": lambda: self.recent_by_trace_id(
                trace_id=payload["trace_id"],
                scope=payload.get("scope", "all"),
                limit=payload.get("limit", 2000),
            ),
        }
        if operation not in handlers:
            raise ValueError(f"unsupported signal operation: {operation}")
        return handlers[operation]()
