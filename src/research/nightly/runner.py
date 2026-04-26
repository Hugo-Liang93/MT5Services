"""Nightly WF 运行器。

调用 BacktestEngine 对每个 (strategy, tf) 组合独立回测，汇总到 NightlyWFReport。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from src.utils.timezone import parse_iso_to_utc
from typing import List, Tuple

from .contracts import (
    NightlyWFConfig,
    NightlyWFReport,
    StrategyMetrics,
)

logger = logging.getLogger(__name__)


def run_nightly_wf(config: NightlyWFConfig) -> NightlyWFReport:
    """执行 nightly WF 回测。

    契约：
      - 单个 (strategy, tf) 失败 → 记入 report.failed_runs，不抛出
      - 基础设施错误（DB 连接、数据加载）→ 直接抛出（bug 必须暴露）
      - workers > 1 时用 ProcessPool 并行；回归检测在汇总后进行

    Returns:
        NightlyWFReport with metrics + failed_runs list.
    """
    started = time.monotonic()

    tasks: List[Tuple[str, str]] = [
        (strategy, tf)
        for strategy in config.strategies
        for tf in config.timeframes
    ]

    metrics: List[StrategyMetrics] = []
    failures: List[str] = []

    if config.workers == 1 or len(tasks) == 1:
        for strategy, tf in tasks:
            try:
                metrics.append(_run_single_combo(
                    symbol=config.symbol, strategy=strategy, tf=tf,
                    start=config.start_date, end=config.end_date,
                ))
            except Exception as exc:
                logger.exception("nightly_wf: %s/%s failed", strategy, tf)
                failures.append(f"{strategy}/{tf}: {exc}")
    else:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        max_workers = min(config.workers, len(tasks))
        ctx = mp.get_context("spawn")
        packed = [
            (config.symbol, s, tf, config.start_date, config.end_date)
            for s, tf in tasks
        ]
        with ProcessPoolExecutor(
            max_workers=max_workers, mp_context=ctx
        ) as pool:
            results = pool.map(_run_single_combo_packed, packed)
            for (strategy, tf), outcome in zip(tasks, results):
                if isinstance(outcome, StrategyMetrics):
                    metrics.append(outcome)
                else:
                    failures.append(f"{strategy}/{tf}: {outcome}")

    elapsed = time.monotonic() - started
    logger.info(
        "nightly_wf: %d combos done in %.1fs (%d failed)",
        len(metrics), elapsed, len(failures),
    )

    return NightlyWFReport(
        generated_at=datetime.now(timezone.utc),
        config=config,
        metrics=tuple(metrics),
        runtime_seconds=elapsed,
        failed_runs=tuple(failures),
    )


# ── 内部 worker ─────────────────────────────────────────────────


def _run_single_combo_packed(args: tuple):
    """ProcessPool 友好的 packer。异常作为 str 返回（便于结果聚合）。"""
    symbol, strategy, tf, start, end = args
    try:
        return _run_single_combo(
            symbol=symbol, strategy=strategy, tf=tf, start=start, end=end,
        )
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _run_single_combo(
    *,
    symbol: str,
    strategy: str,
    tf: str,
    start: str,
    end: str,
) -> StrategyMetrics:
    """执行单个 (strategy, tf) 回测，返回契约 DTO。"""
    from src.backtesting.component_factory import (
        _load_signal_config_snapshot,
        build_backtest_components,
    )
    from src.backtesting.config import get_backtest_defaults
    from src.backtesting.engine import BacktestEngine
    from src.backtesting.models import BacktestConfig

    # Defaults 自动加载 + 过滤优化器专用字段
    optimizer_only = {"search_mode", "max_combinations", "sort_metric"}
    defaults = {
        k: v for k, v in get_backtest_defaults().items() if k not in optimizer_only
    }
    signal_config = _load_signal_config_snapshot()
    strategy_sessions = {
        name: list(sess_list)
        for name, sess_list in signal_config.strategy_sessions.items()
        if sess_list
    }
    strategy_timeframes = {
        name: list(tf_list)
        for name, tf_list in signal_config.strategy_timeframes.items()
        if tf_list
    }
    defaults["trailing_tp_enabled"] = bool(signal_config.trailing_tp_enabled)
    defaults["trailing_tp_activation_atr"] = float(
        signal_config.trailing_tp_activation_atr
    )
    defaults["trailing_tp_trail_atr"] = float(signal_config.trailing_tp_trail_atr)

    bt_config = BacktestConfig.from_flat(
        symbol=symbol,
        timeframe=tf,
        start_time=parse_iso_to_utc(start),
        end_time=parse_iso_to_utc(end),
        strategies=[strategy],
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        **defaults,
    )
    components = build_backtest_components(strategy_names=[strategy])
    engine = BacktestEngine(
        config=bt_config,
        data_loader=components["data_loader"],
        signal_module=components["signal_module"],
        indicator_pipeline=components["pipeline"],
        regime_detector=components["regime_detector"],
        performance_tracker=components.get("performance_tracker"),
    )
    result = engine.run()
    m = result.metrics
    return StrategyMetrics(
        strategy=strategy,
        timeframe=tf,
        trades=m.total_trades,
        win_rate=float(m.win_rate),
        pnl=float(m.total_pnl),
        profit_factor=float(m.profit_factor),
        sharpe=float(m.sharpe_ratio),
        sortino=float(m.sortino_ratio),
        max_dd=float(m.max_drawdown),
        expectancy=float(m.expectancy),
        avg_bars_held=float(m.avg_bars_held),
    )
