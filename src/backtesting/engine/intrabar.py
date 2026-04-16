"""回测 Intrabar 子循环：双 TF 联合回放。

在主循环的每根父 TF bar 内，回放子 TF bars，合成 intrabar 快照，
复用生产 IntrabarTradeCoordinator + IntrabarTradeGuard 进行盘中入场。

设计原则：
- 复用生产组件（coordinator / guard），不重新实现判定逻辑
- 合成逻辑复用 src/market/synthesis.py 纯函数
- 单线程回测无需 RLock（生产组件自带锁，overhead 可忽略）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.clients.mt5_market import OHLC
from src.market.synthesis import build_child_bar_index, synthesize_parent_bar
from src.signals.evaluation.regime import RegimeType
from src.signals.orchestration.intrabar_trade_coordinator import (
    IntrabarTradeCoordinator,
    IntrabarTradingPolicy,
)
from src.trading.execution.intrabar_guard import IntrabarTradeGuard
from src.utils.common import timeframe_seconds

from ..models import IntrabarConfig

if TYPE_CHECKING:
    from .runner import BacktestEngine

logger = logging.getLogger(__name__)


@dataclass
class IntrabarContext:
    """回测 Intrabar 运行时上下文（per BacktestEngine 实例）。

    持有 coordinator / guard / 子 TF 数据索引等，
    在 BacktestEngine.__init__ 中根据 IntrabarConfig 构建。
    """

    config: IntrabarConfig
    coordinator: IntrabarTradeCoordinator
    guard: IntrabarTradeGuard
    child_tf: str  # 子 TF 名称（如 "M5"）
    # intrabar scope 需要计算的指标子集（与生产 intrabar_required_indicators 对齐）
    intrabar_indicator_names: List[str] = field(default_factory=list)
    # 子 TF bars 按父 TF bar 开盘时间分组
    child_bar_index: Dict[datetime, List[OHLC]] = field(default_factory=dict)
    # 期望的子 bar 数量（用于覆盖率检测）
    expected_child_count: int = 0
    # 统计
    total_intrabar_evaluations: int = 0
    total_armed_signals: int = 0
    total_intrabar_entries: int = 0
    skipped_low_coverage: int = 0


def build_intrabar_context(
    config: IntrabarConfig,
    parent_tf: str,
    intrabar_strategies: List[str],
) -> IntrabarContext:
    """从配置构建 IntrabarContext。

    Args:
        config: IntrabarConfig 配置。
        parent_tf: 当前回测的父 TF。
        intrabar_strategies: 支持 intrabar scope 的策略名列表。
    """
    child_tf = config.trigger_map.get(parent_tf, "")
    if not child_tf:
        raise ValueError(
            f"No trigger mapping for parent_tf={parent_tf} in "
            f"intrabar.trigger_map={config.trigger_map}"
        )

    enabled_strategies = frozenset(
        config.enabled_strategies if config.enabled_strategies else intrabar_strategies
    )
    policy = IntrabarTradingPolicy(
        min_stable_bars=config.min_stable_bars,
        min_confidence=config.min_confidence,
        enabled_strategies=enabled_strategies,
    )

    parent_secs = timeframe_seconds(parent_tf)
    child_secs = timeframe_seconds(child_tf)
    expected_child_count = parent_secs // child_secs if child_secs > 0 else 0

    return IntrabarContext(
        config=config,
        coordinator=IntrabarTradeCoordinator(policy=policy),
        guard=IntrabarTradeGuard(),
        child_tf=child_tf,
        expected_child_count=expected_child_count,
    )


def preload_child_bars(
    engine: "BacktestEngine",
    ctx: IntrabarContext,
    symbol: str,
    parent_tf: str,
    start_time: datetime,
    end_time: datetime,
) -> None:
    """预加载子 TF bars 并构建时间索引。"""
    child_bars = engine._data_loader.load_child_bars(
        symbol,
        ctx.child_tf,
        start_time,
        end_time,
    )
    if not child_bars:
        logger.warning(
            "Intrabar: no child bars found for %s/%s [%s ~ %s]",
            symbol,
            ctx.child_tf,
            start_time.isoformat(),
            end_time.isoformat(),
        )
        return

    ctx.child_bar_index = build_child_bar_index(child_bars, parent_tf)
    logger.info(
        "Intrabar: loaded %d child bars (%s) → %d parent bar groups (%s)",
        len(child_bars),
        ctx.child_tf,
        len(ctx.child_bar_index),
        parent_tf,
    )


def run_intrabar_sub_loop(
    engine: "BacktestEngine",
    ctx: IntrabarContext,
    parent_bar: OHLC,
    parent_bar_index: int,
    parent_indicators: Dict[str, Dict[str, Any]],
    regime: RegimeType,
    soft_regime_dict: Optional[Dict[str, Any]],
    all_bars: List[OHLC],
    warmup_count: int,
) -> None:
    """在一根父 TF bar 内执行 intrabar 子循环。

    对每根子 TF bar：
    1. 累积合成父 TF intrabar 快照
    2. 计算指标子集
    3. 评估 intrabar 策略
    4. coordinator 稳定性判定
    5. armed → guard 去重 → 执行入场

    Args:
        engine: 回测引擎实例。
        ctx: IntrabarContext 运行时上下文。
        parent_bar: 当前父 TF bar（confirmed）。
        parent_bar_index: 当前 bar 在 test 区间的索引。
        parent_indicators: 当前 bar 的完整指标快照（用于 ATR 参考）。
        regime: 当前 regime。
        soft_regime_dict: 当前 soft regime。
        all_bars: 全部 bars（含 warmup）。
        warmup_count: warmup bar 数量。
    """
    child_bars = ctx.child_bar_index.get(parent_bar.time, [])
    if not child_bars:
        return

    # 覆盖率检查：子 bar 不足时跳过
    if ctx.expected_child_count > 0:
        coverage = len(child_bars) / ctx.expected_child_count
        if coverage < ctx.config.min_coverage_ratio:
            ctx.skipped_low_coverage += 1
            logger.debug(
                "Intrabar: skip %s bar %s (coverage=%.2f < %.2f, child=%d/%d)",
                parent_bar.timeframe,
                parent_bar.time,
                coverage,
                ctx.config.min_coverage_ratio,
                len(child_bars),
                ctx.expected_child_count,
            )
            return

    symbol = parent_bar.symbol
    parent_tf = parent_bar.timeframe

    # ATR 参考：用上一根 confirmed bar 的 ATR（intrabar ATR 不完整）
    atr_ref = 0.0
    atr_data = parent_indicators.get("atr14")
    if isinstance(atr_data, dict):
        atr_ref = float(atr_data.get("atr", 0.0) or 0.0)

    # 逐根子 TF bar 合成 → 评估 → 判定
    accumulated_child: List[OHLC] = []

    for child_bar in child_bars:
        accumulated_child.append(child_bar)

        # 合成当前 intrabar 快照
        synthesized = synthesize_parent_bar(
            accumulated_child,
            symbol,
            parent_tf,
            parent_bar.time,
        )

        # 计算指标（仅 intrabar 所需子集，与生产对齐）
        intrabar_indicators = _compute_intrabar_indicators(
            engine,
            symbol,
            parent_tf,
            synthesized,
            all_bars,
            warmup_count,
            parent_bar_index,
            indicator_names=ctx.intrabar_indicator_names or None,
        )
        if not intrabar_indicators:
            continue

        # 评估 intrabar 策略
        decisions = _evaluate_intrabar_strategies(
            engine,
            ctx,
            symbol,
            parent_tf,
            intrabar_indicators,
            regime,
            soft_regime_dict,
            bar_time=parent_bar.time,
        )
        ctx.total_intrabar_evaluations += len(decisions)

        # coordinator 稳定性判定 + guard 去重 + 入场
        for decision in decisions:
            if decision.direction not in ("buy", "sell"):
                continue

            armed_state = ctx.coordinator.update(
                symbol=symbol,
                parent_tf=parent_tf,
                strategy=decision.strategy,
                direction=decision.direction,
                confidence=decision.confidence,
                parent_bar_time=parent_bar.time,
            )

            if armed_state is not None:
                ctx.total_armed_signals += 1
                _try_intrabar_entry(
                    engine,
                    ctx,
                    decision,
                    synthesized,
                    parent_bar_index,
                    atr_ref,
                    regime,
                    intrabar_indicators,
                )

    # 父 bar 收盘 → 清理 coordinator + guard 状态
    ctx.coordinator.on_parent_bar_close(symbol, parent_tf, parent_bar.time)
    ctx.guard.on_parent_bar_close(symbol, parent_tf, parent_bar.time)


def check_confirmed_coordination(
    ctx: IntrabarContext,
    strategy: str,
    direction: str,
    symbol: str,
    parent_tf: str,
    parent_bar_time: datetime,
) -> bool:
    """Confirmed 链路协调：检查是否有同方向 intrabar 仓位。

    Returns:
        True = 应跳过本次 confirmed 入场（intrabar 已开仓同方向）。
    """
    has_pos, intra_dir = ctx.guard.has_intrabar_position(
        symbol,
        parent_tf,
        strategy,
        parent_bar_time,
    )
    if has_pos and intra_dir == direction:
        logger.debug(
            "Confirmed skip: %s %s already has intrabar %s position at %s",
            strategy,
            symbol,
            direction,
            parent_bar_time,
        )
        return True
    return False


def intrabar_stats(ctx: IntrabarContext) -> Dict[str, Any]:
    """返回 intrabar 回测统计。"""
    return {
        "child_tf": ctx.child_tf,
        "total_intrabar_evaluations": ctx.total_intrabar_evaluations,
        "total_armed_signals": ctx.total_armed_signals,
        "total_intrabar_entries": ctx.total_intrabar_entries,
        "skipped_low_coverage": ctx.skipped_low_coverage,
        "coordinator": ctx.coordinator.status(),
        "guard": ctx.guard.status(),
    }


# ── 内部辅助函数 ──────────────────────────────────────────────────


def _compute_intrabar_indicators(
    engine: "BacktestEngine",
    symbol: str,
    parent_tf: str,
    synthesized: OHLC,
    all_bars: List[OHLC],
    warmup_count: int,
    parent_bar_index: int,
    indicator_names: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """在合成的 intrabar 快照上计算指标。

    使用 warmup bars + N-1 根 confirmed bars + 合成的当前 bar 构成窗口。
    indicator_names 指定 intrabar 子集（与生产 intrabar_required_indicators 对齐），
    为 None 时退化为全量指标（向后兼容）。
    """
    from .indicators import compute_indicators

    # 构建窗口：前 N 根历史 bar + 合成 bar 替代当前 bar
    abs_index = warmup_count + parent_bar_index
    window_start = max(0, abs_index - warmup_count)
    # 取到当前 bar 之前的所有 bar（不含当前 confirmed bar 本身）
    historical_window = list(all_bars[window_start:abs_index])
    # 追加合成 bar
    historical_window.append(synthesized)

    return compute_indicators(
        engine,
        symbol,
        parent_tf,
        historical_window,
        indicator_names=indicator_names,
        use_required_default=(indicator_names is None),
    )


def _evaluate_intrabar_strategies(
    engine: "BacktestEngine",
    ctx: IntrabarContext,
    symbol: str,
    timeframe: str,
    indicators: Dict[str, Dict[str, Any]],
    regime: RegimeType,
    soft_regime_dict: Optional[Dict[str, Any]],
    *,
    bar_time: Optional[Any] = None,
) -> list:
    """评估 intrabar 策略（仅 scope="intrabar" + 白名单策略）。"""
    from .signals import evaluate_strategies

    return evaluate_strategies(
        engine,
        symbol,
        timeframe,
        indicators,
        regime,
        soft_regime_dict,
        scope="intrabar",
        bar_time=bar_time,
    )


def _try_intrabar_entry(
    engine: "BacktestEngine",
    ctx: IntrabarContext,
    decision: Any,
    synthesized_bar: OHLC,
    parent_bar_index: int,
    atr_ref: float,
    regime: RegimeType,
    indicators: Dict[str, Dict[str, Any]],
) -> None:
    """尝试 intrabar 入场（ATR 校验 + guard 去重 + 执行）。"""
    from .signals import execute_entry

    if atr_ref <= 0:
        return

    symbol = synthesized_bar.symbol
    parent_tf = synthesized_bar.timeframe

    # guard 去重
    allowed, reason = ctx.guard.can_trade(
        symbol,
        parent_tf,
        decision.strategy,
        decision.direction,
        synthesized_bar.time,
    )
    if not allowed:
        logger.debug(
            "Intrabar guard blocked: %s %s %s (%s)",
            decision.strategy,
            decision.direction,
            symbol,
            reason,
        )
        return

    # 执行入场（用合成 bar 的 close 作为成交价）
    execute_entry(
        engine,
        decision,
        synthesized_bar,
        parent_bar_index,
        atr_ref,
        regime,
        indicators,
        entry_scope="intrabar",
    )

    # 记录交易
    ctx.guard.record_trade(
        symbol,
        parent_tf,
        decision.strategy,
        decision.direction,
        synthesized_bar.time,
    )
    ctx.total_intrabar_entries += 1
