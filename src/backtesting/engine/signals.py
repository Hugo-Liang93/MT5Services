from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.clients.mt5_market import OHLC
from src.signals.confidence import apply_intrabar_decay
from src.signals.evaluation.regime import RegimeType
from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalDecision
from src.trading.execution.sizing import RegimeSizing, compute_trade_params

from ..models import SignalEvaluation
from .execution_semantics import resolve_trade_parameters

if TYPE_CHECKING:
    from .runner import BacktestEngine, _BacktestSignalState

logger = logging.getLogger(__name__)


def evaluate_strategies(
    engine: "BacktestEngine",
    symbol: str,
    timeframe: str,
    indicators: Dict[str, Dict[str, Any]],
    regime: RegimeType,
    soft_regime_dict: Optional[Dict[str, Any]],
    *,
    scope: str = "confirmed",
    bar_time: Optional[Any] = None,
) -> List[SignalDecision]:
    conf = engine._config.confidence
    metadata: Dict[str, Any] = {MK.REGIME_HARD: regime.value}
    if soft_regime_dict is not None:
        metadata[MK.REGIME_SOFT] = soft_regime_dict
    if not conf.enable_regime_affinity:
        metadata[MK.PRE_COMPUTED_AFFINITY] = 1.0
    if not conf.enable_performance_tracker:
        metadata[MK.SKIP_PERFORMANCE_TRACKER] = True
    if not conf.enable_calibrator:
        metadata[MK.SKIP_CALIBRATOR] = True

    # 注入 MarketStructure 上下文（结构化策略依赖）
    ms_analyzer = getattr(engine, "_market_structure_analyzer", None)
    if ms_analyzer is not None:
        try:
            ms_ctx = ms_analyzer.analyze(
                symbol,
                timeframe,
                event_time=bar_time,
                latest_close=indicators.get("boll20", {}).get("close")
                or indicators.get("donchian20", {}).get("close"),
            )
            if ms_ctx:
                metadata[MK.MARKET_STRUCTURE] = (
                    ms_ctx.to_dict() if hasattr(ms_ctx, "to_dict") else ms_ctx
                )
        except Exception:
            logger.debug("MarketStructure analysis failed in backtest", exc_info=True)

    # 注入 recent_bars（TrendlineTouch / PriceActionReversal 等策略依赖）
    recent_bars_window = getattr(engine, "_recent_bars_window", None)
    if recent_bars_window is not None:
        metadata[MK.RECENT_BARS] = [
            dataclasses.asdict(b) if dataclasses.is_dataclass(b) else b
            for b in recent_bars_window
        ]

    current_sessions: List[str] = []
    if engine._strategy_sessions and engine._session_filter and bar_time is not None:
        current_sessions = engine._session_filter.current_sessions(bar_time)

    decisions: List[SignalDecision] = []
    for strategy_name in engine._target_strategies:
        try:
            # ── Deployment gate：与实盘 PreTradePipeline 同契约 ─────────
            deployment = engine._strategy_deployments.get(strategy_name)
            if deployment is not None:
                # CANDIDATE 已在 _target_strategies 过滤；此处复查兜底 + 处理
                # locked_timeframes / locked_sessions 这类运行期约束
                if not deployment.allows_runtime_evaluation():
                    continue
                if deployment.locked_timeframes:
                    tf_upper = (timeframe or "").strip().upper()
                    if tf_upper not in deployment.locked_timeframes:
                        continue
                if deployment.locked_sessions and current_sessions:
                    if not any(
                        s in deployment.locked_sessions for s in current_sessions
                    ):
                        continue

            allowed_sessions = engine._strategy_sessions.get(strategy_name, ())
            if allowed_sessions and current_sessions:
                if not any(s in allowed_sessions for s in current_sessions):
                    continue
            capability = engine.strategy_capability(strategy_name)
            if capability is None:
                logger.warning("Strategy capability missing for %s", strategy_name)
                continue
            if scope not in capability.valid_scopes:
                continue
            required = capability.needed_indicators
            missing = [ind for ind in required if ind not in indicators]
            if missing:
                continue

            scoped_indicators = {
                ind: indicators[ind] for ind in required if ind in indicators
            }

            # HTF fallback：当策略在 TF=X 上运行且需要 HTF=X 的数据时，
            # 用当前 TF 的 indicators 作为 fallback（避免 H1 策略 HTF=H1 时空）
            htf_data = dict(engine._htf_indicator_data)
            if timeframe not in htf_data:
                htf_data[timeframe] = indicators

            decision = engine._signal_module.evaluate(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy_name,
                indicators=scoped_indicators,
                metadata=metadata,
                persist=False,
                htf_indicators=htf_data,
            )

            decision = apply_intrabar_decay(
                decision, scope, engine._intrabar_confidence_factor
            )
            decisions.append(decision)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Strategy %s evaluation failed: %s", strategy_name, exc)
        except Exception:
            logger.warning("Strategy %s unexpected error", strategy_name, exc_info=True)
    return decisions


def update_state_machine(
    engine: "BacktestEngine",
    strategy: str,
    action: str,
    confidence: float,
) -> bool:
    del confidence
    state = engine._signal_states.get(strategy)
    if state is None:
        state = engine._state_factory()
        engine._signal_states[strategy] = state

    if action == state.current_action:
        state.stable_bars += 1
    else:
        state.current_action = action
        state.stable_bars = 1
        state.armed = False

    if state.stable_bars >= engine._config.min_preview_stable_bars:
        state.armed = True

    return state.armed and action in ("buy", "sell")


def process_decision(
    engine: "BacktestEngine",
    decision: SignalDecision,
    bar: OHLC,
    bar_index: int,
    indicators: Dict[str, Dict[str, Any]],
    regime: RegimeType,
) -> None:
    if decision.direction not in ("buy", "sell"):
        if engine._config.enable_state_machine:
            update_state_machine(
                engine, decision.strategy, decision.direction, decision.confidence
            )
        return
    if decision.confidence < engine._config.confidence.min_confidence:
        return

    if engine._circuit_breaker is not None and engine._circuit_breaker.is_paused(
        bar_index
    ):
        return

    if engine._config.enable_state_machine:
        armed = update_state_machine(
            engine, decision.strategy, decision.direction, decision.confidence
        )
        if not armed:
            logger.debug(
                "State machine: %s %s not armed yet (stable_bars=%d/%d)",
                decision.strategy,
                decision.direction,
                engine._signal_states[decision.strategy].stable_bars,
                engine._config.min_preview_stable_bars,
            )
            return

    for pos in engine._portfolio._open_positions:
        if pos.strategy == decision.strategy and pos.direction == decision.direction:
            return

    # ── Deployment gate：max_live_positions / require_pending_entry ─
    deployment = engine._strategy_deployments.get(decision.strategy)
    if deployment is not None and deployment.max_live_positions is not None:
        strategy_open = sum(
            1
            for pos in engine._portfolio._open_positions
            if pos.strategy == decision.strategy
        )
        if strategy_open >= deployment.max_live_positions:
            engine.record_execution_rejection("deployment_max_live_positions")
            return

    atr_value = indicators.get("atr14", {}).get("atr", 0.0)
    if atr_value <= 0:
        return

    if engine._pending_entry_enabled:
        entry_spec = decision.metadata.get(MK.ENTRY_SPEC, {})
        entry_type = entry_spec.get("entry_type", "market")

        # require_pending_entry：部署要求 pending 但策略输出 market → 拒绝
        if (
            deployment is not None
            and deployment.require_pending_entry
            and entry_type == "market"
        ):
            engine.record_execution_rejection("deployment_requires_pending_entry")
            return

        if entry_type == "market":
            # 市价策略：跳过 pending，直接在 bar.close 入场
            execute_entry(
                engine, decision, bar, bar_index, atr_value, regime, indicators
            )
            return

        if entry_type not in ("limit", "stop"):
            logger.warning(
                "Unknown entry_type %s for %s, fallback to market",
                entry_type,
                decision.strategy,
            )
            execute_entry(
                engine, decision, bar, bar_index, atr_value, regime, indicators
            )
            return

        suggested_price = entry_spec.get("entry_price")
        zone_atr = entry_spec.get("entry_zone_atr", 0.3)

        ref_price = float(suggested_price) if suggested_price is not None else bar.close
        half_zone = zone_atr * atr_value
        entry_low = round(ref_price - half_zone, 2)
        entry_high = round(ref_price + half_zone, 2)

        expiry_bar = bar_index + engine._config.pending_entry.expiry_bars
        key = f"{decision.strategy}_{decision.direction}"
        engine._pending_entries[key] = (
            decision,
            entry_type,
            entry_low,
            entry_high,
            expiry_bar,
        )
        return

    execute_entry(engine, decision, bar, bar_index, atr_value, regime, indicators)


def _resolve_pending_fill(
    direction: str,
    entry_type: str,
    entry_low: float,
    entry_high: float,
    bar: OHLC,
) -> Optional[float]:
    """按 direction + entry_type 判触发方向并返回真实可成交价格。

    避免用 bar.close 成交造成的 look-ahead：pending 必须按其挂单语义在
    "第一次触及触发边界"的价格成交；若 bar 开盘已越过该边界（跳空），
    则按 bar.open 成交（realistic broker fill）。

    返回 None 表示本 bar 未触发。

    | direction | entry_type | 触发边界        | 触发条件             | 成交价            |
    |-----------|------------|-----------------|----------------------|-------------------|
    | buy       | limit      | entry_high (上) | bar.low  <= entry_high | min(open, high-edge) |
    | buy       | stop       | entry_low  (下) | bar.high >= entry_low  | max(open, low-edge)  |
    | sell      | limit      | entry_low  (下) | bar.high >= entry_low  | max(open, low-edge)  |
    | sell      | stop       | entry_high (上) | bar.low  <= entry_high | min(open, high-edge) |
    """
    # buy-limit / sell-stop 共享"从上方下穿 entry_high"的触发语义
    if (direction == "buy" and entry_type == "limit") or (
        direction == "sell" and entry_type == "stop"
    ):
        if bar.low > entry_high:
            return None
        return min(bar.open, entry_high)

    # buy-stop / sell-limit 共享"从下方上穿 entry_low"的触发语义
    if (direction == "buy" and entry_type == "stop") or (
        direction == "sell" and entry_type == "limit"
    ):
        if bar.high < entry_low:
            return None
        return max(bar.open, entry_low)

    return None


def check_pending_entries(
    engine: "BacktestEngine",
    bar: OHLC,
    bar_index: int,
    indicators: Dict[str, Dict[str, Any]],
    regime: RegimeType,
) -> None:
    if not engine._pending_entries:
        return

    filled_keys: List[str] = []
    for key, (
        decision,
        entry_type,
        entry_low,
        entry_high,
        expiry_bar,
    ) in engine._pending_entries.items():
        if bar_index > expiry_bar:
            logger.debug(
                "Pending entry expired: %s %s at bar %d (expiry=%d)",
                decision.strategy,
                decision.direction,
                bar_index,
                expiry_bar,
            )
            filled_keys.append(key)
            continue

        fill_price = _resolve_pending_fill(
            decision.direction,
            entry_type,
            entry_low,
            entry_high,
            bar,
        )
        if fill_price is not None:
            atr_value = indicators.get("atr14", {}).get("atr", 0.0)
            if atr_value > 0:
                execute_entry(
                    engine,
                    decision,
                    bar,
                    bar_index,
                    atr_value,
                    regime,
                    indicators,
                    fill_price=fill_price,
                )
            filled_keys.append(key)

    for key in filled_keys:
        engine._pending_entries.pop(key, None)


def execute_entry(
    engine: "BacktestEngine",
    decision: SignalDecision,
    bar: OHLC,
    bar_index: int,
    atr_value: float,
    regime: RegimeType,
    indicators: Dict[str, Dict[str, Any]] | None = None,
    fill_price: Optional[float] = None,
    entry_scope: str = "confirmed",
) -> None:
    del indicators
    pos = engine._config.position
    # 策略 exit_spec 可覆盖全局 SL/TP 倍数（回测默认 1.5 / 3.0）
    _exit_spec = decision.metadata.get(MK.EXIT_SPEC, {})

    # Pending 单按挂单触发边界成交，market 单仍用 bar.close
    effective_price = float(fill_price) if fill_price is not None else bar.close

    try:
        trade_params = compute_trade_params(
            action=decision.direction,
            current_price=effective_price,
            atr_value=atr_value,
            account_balance=engine._portfolio.current_balance,
            timeframe=engine._config.timeframe,
            risk_percent=pos.risk_percent,
            sl_atr_multiplier=_exit_spec.get("sl_atr") or 1.5,
            tp_atr_multiplier=_exit_spec.get("tp_atr") or 3.0,
            min_volume=pos.min_volume,
            max_volume=pos.max_volume,
            contract_size=pos.contract_size,
            regime=regime.value,
            regime_sizing=RegimeSizing(
                tp_trending=pos.regime_tp_trending,
                tp_ranging=pos.regime_tp_ranging,
                tp_breakout=pos.regime_tp_breakout,
                tp_uncertain=pos.regime_tp_uncertain,
                sl_trending=pos.regime_sl_trending,
                sl_ranging=pos.regime_sl_ranging,
                sl_breakout=pos.regime_sl_breakout,
                sl_uncertain=pos.regime_sl_uncertain,
            ),
        )
    except ValueError as exc:
        engine.record_execution_rejection("trade_params_error")
        logger.debug(
            "Trade params computation failed for %s %s: %s",
            decision.strategy,
            decision.direction,
            exc,
        )
        return

    execution_resolution = resolve_trade_parameters(
        engine._config,
        trade_params,
        account_balance=engine._portfolio.current_balance,
    )
    if not execution_resolution.accepted or execution_resolution.trade_params is None:
        engine.record_execution_rejection(execution_resolution.reason)
        logger.debug(
            "Entry blocked by execution semantics for %s: %s",
            decision.strategy,
            execution_resolution.reason,
        )
        return
    trade_params = execution_resolution.trade_params

    allowed, reason = engine._portfolio.can_open_position(bar, trade_params)
    if not allowed:
        engine.record_execution_rejection(reason)
        logger.debug(
            "Entry blocked by portfolio risk guard for %s: %s",
            decision.strategy,
            reason,
        )
        return

    # 获取策略 category（用于 regime-aware 出场 profile 查找）
    strategy_obj = engine._signal_module.get_strategy(decision.strategy)
    strategy_category = getattr(strategy_obj, "category", "") if strategy_obj else ""

    opened = engine._portfolio.open_position(
        strategy=decision.strategy,
        action=decision.direction,
        bar=bar,
        trade_params=trade_params,
        regime=regime.value,
        confidence=decision.confidence,
        bar_index=bar_index,
        atr_at_entry=atr_value,
        strategy_category=strategy_category,
        timeframe=engine._config.timeframe,
        exit_spec=_exit_spec or None,
        fill_price=fill_price,
        entry_scope=entry_scope,
    )
    if opened:
        engine.record_entry_acceptance()
    else:
        engine.record_execution_rejection("portfolio_open_failed")


def record_evaluation(
    engine: "BacktestEngine",
    bar: OHLC,
    bar_index: int,
    strategy: str,
    action: str,
    confidence: float,
    regime: str,
    *,
    filtered: bool = False,
    filter_reason: str = "",
) -> None:
    if len(engine._signal_evaluations) >= engine._config.max_signal_evaluations:
        return
    dedup_key = (bar_index, strategy)
    if dedup_key in engine._recorded_evals:
        return
    engine._recorded_evals.add(dedup_key)

    eval_record = SignalEvaluation(
        bar_time=bar.time,
        strategy=strategy,
        direction=action,
        confidence=confidence,
        regime=regime,
        price_at_signal=bar.close,
        bars_to_evaluate=engine._bars_to_evaluate,
        filtered=filtered,
        filter_reason=filter_reason,
    )

    if action in ("buy", "sell") and not filtered:
        target_index = bar_index + engine._bars_to_evaluate
        engine._pending_evaluations.setdefault(target_index, []).append(eval_record)
    else:
        engine._signal_evaluations.append(eval_record)


def fill_evaluation(
    ev: SignalEvaluation,
    exit_price: float,
    *,
    incomplete: bool = False,
) -> SignalEvaluation:
    if ev.direction == "buy":
        pnl_pct = (exit_price - ev.price_at_signal) / ev.price_at_signal * 100
        won = exit_price > ev.price_at_signal
    else:
        pnl_pct = (ev.price_at_signal - exit_price) / ev.price_at_signal * 100
        won = exit_price < ev.price_at_signal

    return SignalEvaluation(
        bar_time=ev.bar_time,
        strategy=ev.strategy,
        direction=ev.direction,
        confidence=ev.confidence,
        regime=ev.regime,
        price_at_signal=ev.price_at_signal,
        price_after_n_bars=exit_price,
        bars_to_evaluate=ev.bars_to_evaluate,
        won=won,
        pnl_pct=round(pnl_pct, 4),
        filtered=ev.filtered,
        filter_reason=ev.filter_reason,
        incomplete=incomplete,
    )


def backfill_evaluations(
    engine: "BacktestEngine", current_bar_index: int, current_price: float
) -> None:
    if current_bar_index not in engine._pending_evaluations:
        return

    pending_list = engine._pending_evaluations.pop(current_bar_index)
    for ev in pending_list:
        engine._signal_evaluations.append(fill_evaluation(ev, current_price))


def flush_pending_evaluations(engine: "BacktestEngine", last_price: float) -> None:
    for _target_index, pending_list in sorted(engine._pending_evaluations.items()):
        for ev in pending_list:
            engine._signal_evaluations.append(
                fill_evaluation(ev, last_price, incomplete=True)
            )
    engine._pending_evaluations.clear()
