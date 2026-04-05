# docstring removed during encoding normalization

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _cleanup_components(components: Dict[str, Any]) -> None:
    # docstring removed during encoding normalization
    from .component_factory import build_backtest_components

    strategy_params = getattr(args, "strategy_params", None) or None
    return build_backtest_components(strategy_params=strategy_params)


def _load_strategy_scope_overrides() -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    try:
        from .component_factory import _load_signal_config_snapshot

        signal_config = _load_signal_config_snapshot()
    except Exception:
        return {}, {}

    def _normalize(mapping: Any) -> Dict[str, List[str]]:
        normalized: Dict[str, List[str]] = {}
        for name, values in (mapping or {}).items():
            items = list(values) if isinstance(values, (list, tuple, set)) else [values]
            cleaned = [str(item).strip() for item in items if str(item).strip()]
            if cleaned:
                normalized[str(name)] = cleaned
        return normalized

    return (
        _normalize(getattr(signal_config, "strategy_timeframes", {}) or {}),
        _normalize(getattr(signal_config, "strategy_sessions", {}) or {}),
    )


def cmd_run(args: argparse.Namespace) -> None:
    # docstring removed during encoding normalization
    from .config import get_backtest_defaults
    from .engine import BacktestEngine
    from .models import BacktestConfig
    from .report import format_summary

    strategies = args.strategies.split(",") if args.strategies else None
    ini_defaults = get_backtest_defaults()
    strategy_timeframes, strategy_sessions = _load_strategy_scope_overrides()

    config = BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_time=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        strategies=strategies,
        strategy_timeframes=strategy_timeframes,
        strategy_sessions=strategy_sessions,
        initial_balance=args.balance,
        min_confidence=args.min_confidence,
        warmup_bars=args.warmup,
        filters_enabled=not args.no_filters,
        commission_per_lot=ini_defaults.get("commission_per_lot", 0.0),
        slippage_points=ini_defaults.get("slippage_points", 0.0),
        contract_size=ini_defaults.get("contract_size", 100.0),
        risk_percent=ini_defaults.get("risk_percent", 1.0),
        max_positions=ini_defaults.get("max_positions", 3),
        max_signal_evaluations=ini_defaults.get("max_signal_evaluations", 50000),
        filter_session_enabled=ini_defaults.get("filter_session_enabled", True),
        filter_allowed_sessions=ini_defaults.get("filter_allowed_sessions", "london,new_york"),
        filter_session_transition_enabled=ini_defaults.get(
            "filter_session_transition_enabled", True
        ),
        filter_session_transition_cooldown=ini_defaults.get(
            "filter_session_transition_cooldown", 15
        ),
        filter_volatility_enabled=ini_defaults.get("filter_volatility_enabled", True),
        filter_volatility_spike_multiplier=ini_defaults.get(
            "filter_volatility_spike_multiplier", 2.5
        ),
        filter_economic_enabled=not getattr(args, "no_economic", False),
        regime_tp_trending=ini_defaults.get("regime_tp_trending", 1.20),
        regime_tp_ranging=ini_defaults.get("regime_tp_ranging", 0.80),
        regime_tp_breakout=ini_defaults.get("regime_tp_breakout", 1.10),
        regime_tp_uncertain=ini_defaults.get("regime_tp_uncertain", 1.00),
        regime_sl_trending=ini_defaults.get("regime_sl_trending", 1.00),
        regime_sl_ranging=ini_defaults.get("regime_sl_ranging", 0.90),
        regime_sl_breakout=ini_defaults.get("regime_sl_breakout", 1.10),
        regime_sl_uncertain=ini_defaults.get("regime_sl_uncertain", 1.00),
        **(
            {"trailing_atr_multiplier": args.trailing}
            if getattr(args, "trailing", None)
            else {}
        ),
        **(
            {"breakeven_atr_threshold": args.breakeven}
            if getattr(args, "breakeven", None)
            else {}
        ),
        trailing_tp_enabled=ini_defaults.get("trailing_tp_enabled", False),
        trailing_tp_activation_atr=ini_defaults.get("trailing_tp_activation_atr", 1.5),
        trailing_tp_trail_atr=ini_defaults.get("trailing_tp_trail_atr", 0.8),
        circuit_breaker_enabled=ini_defaults.get("circuit_breaker_enabled", False),
        circuit_breaker_max_consecutive_losses=ini_defaults.get(
            "circuit_breaker_max_consecutive_losses", 5
        ),
        circuit_breaker_cooldown_bars=ini_defaults.get(
            "circuit_breaker_cooldown_bars", 20
        ),
    )

    _sl_tp_backup: Optional[Dict] = None
    if getattr(args, "sl_mult", None) is not None or getattr(args, "tp_mult", None) is not None:
        import copy

        from src.trading.execution import TIMEFRAME_SL_TP

        tf = args.timeframe.upper()
        if tf in TIMEFRAME_SL_TP:
            _sl_tp_backup = copy.deepcopy(TIMEFRAME_SL_TP)
            if args.sl_mult is not None:
                TIMEFRAME_SL_TP[tf]["sl_atr_mult"] = args.sl_mult
            if args.tp_mult is not None:
                TIMEFRAME_SL_TP[tf]["tp_atr_mult"] = args.tp_mult
            logger.info(
                "CLI override: %s SL/TP = %.1f/%.1f ATR (will restore after backtest)",
                tf,
                TIMEFRAME_SL_TP[tf]["sl_atr_mult"],
                TIMEFRAME_SL_TP[tf]["tp_atr_mult"],
            )

    components = _build_components(args)
    try:
        engine = BacktestEngine(
            config=config,
            data_loader=components["data_loader"],
            signal_module=components["signal_module"],
            indicator_pipeline=components["pipeline"],
            regime_detector=components["regime_detector"],
            voting_engine=components.get("voting_engine"),
            voting_group_engines=components.get("voting_group_engines"),
            performance_tracker=components.get("performance_tracker"),
        )

        result = engine.run()
        print(format_summary(result))

        if not args.no_persist:
            _persist_result(result, components.get("writer"))

        if args.output:
            from .report import result_to_json

            with open(args.output, "w", encoding="utf-8") as f:
                f.write(result_to_json(result))
            print(f": {args.output}")
    finally:
        if _sl_tp_backup is not None:
            from src.trading.execution import TIMEFRAME_SL_TP

            TIMEFRAME_SL_TP.clear()
            TIMEFRAME_SL_TP.update(_sl_tp_backup)
        _cleanup_components(components)

def cmd_optimize(args: argparse.Namespace) -> None:
    # docstring removed during encoding normalization
    from .config import get_backtest_defaults
    from .models import BacktestConfig, ParameterSpace
    from .optimizer import ParameterOptimizer, build_signal_module_with_overrides
    from .report import format_optimization_summary

    strategies = args.strategies.split(",") if args.strategies else None
    ini_defaults = get_backtest_defaults()
    strategy_timeframes, strategy_sessions = _load_strategy_scope_overrides()
    config = BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_time=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
        end_time=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        strategies=strategies,
        strategy_timeframes=strategy_timeframes,
        strategy_sessions=strategy_sessions,
        initial_balance=args.balance,
        min_confidence=args.min_confidence,
        warmup_bars=args.warmup,
        commission_per_lot=ini_defaults.get("commission_per_lot", 0.0),
        slippage_points=ini_defaults.get("slippage_points", 0.0),
        contract_size=ini_defaults.get("contract_size", 100.0),
        risk_percent=ini_defaults.get("risk_percent", 1.0),
        max_positions=ini_defaults.get("max_positions", 3),
        regime_tp_trending=ini_defaults.get("regime_tp_trending", 1.20),
        regime_tp_ranging=ini_defaults.get("regime_tp_ranging", 0.80),
        regime_tp_breakout=ini_defaults.get("regime_tp_breakout", 1.10),
        regime_tp_uncertain=ini_defaults.get("regime_tp_uncertain", 1.00),
        regime_sl_trending=ini_defaults.get("regime_sl_trending", 1.00),
        regime_sl_ranging=ini_defaults.get("regime_sl_ranging", 0.90),
        regime_sl_breakout=ini_defaults.get("regime_sl_breakout", 1.10),
        regime_sl_uncertain=ini_defaults.get("regime_sl_uncertain", 1.00),
    )

    param_dict: Dict[str, List[Any]] = {}
    for p in args.param:
        key, values = _parse_param(p)
        param_dict[key] = values

    param_space = ParameterSpace(
        strategy_params=param_dict,
        search_mode=args.mode,
        max_combinations=args.max_combos,
    )

    components = _build_components(args)
    try:
        base_module = components["signal_module"]

        def module_factory(params: Dict[str, Any]):  # type: ignore[no-untyped-def]
            return build_signal_module_with_overrides(base_module, params)

        optimizer = ParameterOptimizer(
            base_config=config,
            param_space=param_space,
            data_loader=components["data_loader"],
            indicator_pipeline=components["pipeline"],
            signal_module_factory=module_factory,
            regime_detector=components["regime_detector"],
            sort_metric=args.sort,
        )

        def progress(current: int, total: int, result: Any) -> None:
            print(
                f"  [{current}/{total}] "
                f"Sharpe={result.metrics.sharpe_ratio:.4f} "
                f"Win={result.metrics.win_rate * 100:.1f}% "
                f"PnL={result.metrics.total_pnl:+.2f}"
            )

        results = optimizer.run(progress_callback=progress)
        print(format_optimization_summary(results))
    finally:
        _cleanup_components(components)

def cmd_compare_tf(args: argparse.Namespace) -> None:
    # docstring removed during encoding normalization
    from .config import get_backtest_defaults
    from .engine import BacktestEngine
    from .models import BacktestConfig
    from .report import format_timeframe_comparison

    strategies = args.strategies.split(",") if args.strategies else None
    timeframes = [tf.strip().upper() for tf in args.timeframes.split(",") if tf.strip()]
    if not timeframes:
        raise ValueError("timeframes  M1,M5,M15,H1 ")

    ini_defaults = get_backtest_defaults()
    strategy_timeframes, strategy_sessions = _load_strategy_scope_overrides()
    results_by_tf: Dict[str, Any] = {}
    for timeframe in timeframes:
        components = _build_components(args)
        try:
            config = BacktestConfig(
                symbol=args.symbol,
                timeframe=timeframe,
                start_time=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
                end_time=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
                strategies=strategies,
                strategy_timeframes=strategy_timeframes,
                strategy_sessions=strategy_sessions,
                initial_balance=args.balance,
                min_confidence=args.min_confidence,
                warmup_bars=args.warmup,
                filters_enabled=not args.no_filters,
                commission_per_lot=ini_defaults.get("commission_per_lot", 0.0),
                slippage_points=ini_defaults.get("slippage_points", 0.0),
                contract_size=ini_defaults.get("contract_size", 100.0),
                risk_percent=ini_defaults.get("risk_percent", 1.0),
                max_positions=ini_defaults.get("max_positions", 3),
                max_signal_evaluations=ini_defaults.get("max_signal_evaluations", 50000),
                filter_session_enabled=ini_defaults.get("filter_session_enabled", True),
                filter_allowed_sessions=ini_defaults.get("filter_allowed_sessions", "london,new_york"),
                filter_session_transition_enabled=ini_defaults.get(
                    "filter_session_transition_enabled", True
                ),
                filter_session_transition_cooldown=ini_defaults.get(
                    "filter_session_transition_cooldown", 15
                ),
                filter_volatility_enabled=ini_defaults.get("filter_volatility_enabled", True),
                filter_volatility_spike_multiplier=ini_defaults.get(
                    "filter_volatility_spike_multiplier", 2.5
                ),
                regime_tp_trending=ini_defaults.get("regime_tp_trending", 1.20),
                regime_tp_ranging=ini_defaults.get("regime_tp_ranging", 0.80),
                regime_tp_breakout=ini_defaults.get("regime_tp_breakout", 1.10),
                regime_tp_uncertain=ini_defaults.get("regime_tp_uncertain", 1.00),
                regime_sl_trending=ini_defaults.get("regime_sl_trending", 1.00),
                regime_sl_ranging=ini_defaults.get("regime_sl_ranging", 0.90),
                regime_sl_breakout=ini_defaults.get("regime_sl_breakout", 1.10),
                regime_sl_uncertain=ini_defaults.get("regime_sl_uncertain", 1.00),
            )
            engine = BacktestEngine(
                config=config,
                data_loader=components["data_loader"],
                signal_module=components["signal_module"],
                indicator_pipeline=components["pipeline"],
                regime_detector=components["regime_detector"],
                voting_engine=components.get("voting_engine"),
                voting_group_engines=components.get("voting_group_engines"),
                performance_tracker=components.get("performance_tracker"),
            )
            logger.info("Running baseline backtest for timeframe=%s", timeframe)
            result = engine.run()
            results_by_tf[timeframe] = result
        finally:
            _cleanup_components(components)
    print(format_timeframe_comparison(results_by_tf))

def main() -> None:
    # docstring removed during encoding normalization
    try:
        from src.persistence.repositories.backtest_repo import BacktestRepository

        if writer is None:
            from src.config.database import load_db_settings
            from src.persistence.db import TimescaleWriter

            db_config = load_db_settings()
            writer = TimescaleWriter(settings=db_config)

        repo = BacktestRepository(writer)
        repo.ensure_schema()
        repo.save_result(result)
        print(f": {result.run_id}")
    except Exception as e:
        logger.warning("? %s", e, exc_info=True)
        print(f": ?- {e}")


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    # docstring removed during encoding normalization
    parser.add_argument("--symbol", required=True, help=" (?XAUUSD)")
    parser.add_argument("--timeframe", required=True, help=" (?M5)")
    parser.add_argument("--start", required=True, help=" (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help=" (YYYY-MM-DD)")
    parser.add_argument(
        "--strategies", type=str, default=None, help=" ()"
    )
    parser.add_argument(
        "--balance", type=float, default=10000.0, help=" (: 10000)"
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.55,
        help=" (: 0.55)",
    )
    parser.add_argument(
        "--warmup", type=int, default=200, help=" bar  (: 200)"
    )
    parser.add_argument(
        "--no-filters",
        action="store_true",
        help="?()",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="",
    )
    parser.add_argument(
        "--no-economic",
        action="store_true",
        help="",
    )
    parser.add_argument(
        "--sl-mult", type=float, default=None,
        help="Override stop-loss ATR multiplier, e.g. 2.0",
    )
    parser.add_argument(
        "--tp-mult", type=float, default=None,
        help="Override take-profit ATR multiplier, e.g. 3.0",
    )
    parser.add_argument(
        "--trailing", type=float, default=None,
        help="Override trailing-stop ATR multiplier, e.g. 0.8",
    )
    parser.add_argument(
        "--breakeven", type=float, default=None,
        help="Override breakeven ATR threshold, e.g. 0.8",
    )


if __name__ == "__main__":
    main()
