from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.clients.mt5_market import OHLC
from src.signals.confidence import apply_htf_alignment, apply_intrabar_decay
from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalDecision
from src.trading.pending import compute_entry_zone
from src.trading.execution import RegimeSizing, compute_trade_params

from ..models import SignalEvaluation

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
    metadata: Dict[str, Any] = {"_regime": regime.value}
    if soft_regime_dict is not None:
        metadata["_soft_regime"] = soft_regime_dict
    if not conf.enable_regime_affinity:
        metadata["_pre_computed_affinity"] = 1.0
    if not conf.enable_performance_tracker:
        metadata["_skip_performance_tracker"] = True
    if not conf.enable_calibrator:
        metadata["_skip_calibrator"] = True

    current_sessions: List[str] = []
    if engine._strategy_sessions and engine._session_filter and bar_time is not None:
        current_sessions = engine._session_filter.current_sessions(bar_time)

    decisions: List[SignalDecision] = []
    for strategy_name in engine._target_strategies:
        try:
            allowed_sessions = engine._strategy_sessions.get(strategy_name, ())
            if allowed_sessions and current_sessions:
                if not any(s in allowed_sessions for s in current_sessions):
                    continue

            required = engine._signal_module.strategy_requirements(strategy_name)
            missing = [ind for ind in required if ind not in indicators]
            if missing:
                continue

            scoped_indicators = {
                ind: indicators[ind] for ind in required if ind in indicators
            }

            decision = engine._signal_module.evaluate(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy_name,
                indicators=scoped_indicators,
                metadata=metadata,
                persist=False,
                htf_indicators=engine._htf_indicator_data,
            )

            if conf.enable_htf_alignment and engine._htf_direction_fn is not None:
                htf_dir = engine._htf_direction_fn(symbol, timeframe)
                decision = apply_htf_alignment(
                    decision,
                    htf_direction=htf_dir,
                    alignment_boost=engine._htf_alignment_boost,
                    conflict_penalty=engine._htf_conflict_penalty,
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

    if engine._circuit_breaker is not None and engine._circuit_breaker.is_paused(bar_index):
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

    atr_value = indicators.get("atr14", {}).get("atr", 0.0)
    if atr_value <= 0:
        return

    if engine._pending_entry_enabled:
        from src.trading.pending import _CATEGORY_ZONE_MODE

        category = decision.metadata.get("category", "trend")
        zone_mode = _CATEGORY_ZONE_MODE.get(category, "symmetric")
        entry_low, entry_high = compute_entry_zone(
            action=decision.direction,
            close_price=bar.close,
            atr=atr_value,
            zone_mode=zone_mode,
            config=engine._pending_entry_config,
            strategy_name=decision.strategy,
            category=category,
            indicators=indicators,
            timeframe=engine._config.timeframe,
        )
        expiry_bar = bar_index + engine._config.pending_entry.expiry_bars
        key = f"{decision.strategy}_{decision.direction}"
        engine._pending_entries[key] = (decision, entry_low, entry_high, expiry_bar)
        return

    execute_entry(engine, decision, bar, bar_index, atr_value, regime, indicators)


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
    for key, (decision, entry_low, entry_high, expiry_bar) in engine._pending_entries.items():
        if bar_index > expiry_bar:
            logger.debug(
                "Pending entry expired: %s %s at bar %d (expiry=%d)",
                decision.strategy, decision.direction, bar_index, expiry_bar,
            )
            filled_keys.append(key)
            continue

        price_in_zone = bar.low <= entry_high and bar.high >= entry_low
        if price_in_zone:
            atr_value = indicators.get("atr14", {}).get("atr", 0.0)
            if atr_value > 0:
                execute_entry(engine, decision, bar, bar_index, atr_value, regime, indicators)
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
) -> None:
    del indicators
    pos = engine._config.position
    try:
        trade_params = compute_trade_params(
            action=decision.direction,
            current_price=bar.close,
            atr_value=atr_value,
            account_balance=engine._portfolio.current_balance,
            timeframe=engine._config.timeframe,
            risk_percent=pos.risk_percent,
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
        logger.debug(
            "Trade params computation failed for %s %s: %s",
            decision.strategy, decision.direction, exc,
        )
        return

    allowed, reason = engine._portfolio.can_open_position(bar, trade_params)
    if not allowed:
        logger.debug(
            "Entry blocked by portfolio risk guard for %s: %s",
            decision.strategy,
            reason,
        )
        return

    # 获取策略 category（用于 regime-aware 出场 profile 查找）
    strategy_obj = engine._signal_module.get_strategy(decision.strategy)
    strategy_category = getattr(strategy_obj, "category", "") if strategy_obj else ""

    engine._portfolio.open_position(
        strategy=decision.strategy,
        action=decision.direction,
        bar=bar,
        trade_params=trade_params,
        regime=regime.value,
        confidence=decision.confidence,
        bar_index=bar_index,
        atr_at_entry=atr_value,
        strategy_category=strategy_category,
    )


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
