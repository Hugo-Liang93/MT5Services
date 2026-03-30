"""持仓管理纯逻辑：breakeven / trailing stop / 日终平仓 / 指标驱动出场判定。

这些函数不依赖 MT5 API，可被实盘 PositionManager 和回测 PortfolioTracker 共用。
实盘调用后执行 _modify_sl()（MT5 API），回测调用后直接修改内存中的 SL。

设计原则：回测使用实盘方法，不重新实现，避免模拟失真。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class BreakevenResult:
    """Breakeven 判定结果。"""

    should_apply: bool
    new_stop_loss: Optional[float] = None


@dataclass
class TrailingStopResult:
    """Trailing stop 判定结果。"""

    should_update: bool
    new_stop_loss: Optional[float] = None


@dataclass
class EndOfDayResult:
    """日终平仓判定结果。"""

    should_close: bool


def check_breakeven(
    action: str,
    entry_price: float,
    current_price: float,
    atr_at_entry: float,
    breakeven_atr_threshold: float,
    already_applied: bool,
) -> BreakevenResult:
    """判断是否应将止损移至保本价。

    当价格朝持仓方向移动 >= atr_at_entry * breakeven_atr_threshold 时触发。

    Args:
        action: "buy" 或 "sell"
        entry_price: 入场价
        current_price: 当前价格
        atr_at_entry: 入场时的 ATR 值
        breakeven_atr_threshold: ATR 倍数阈值
        already_applied: 是否已经应用过 breakeven

    Returns:
        BreakevenResult，should_apply=True 时 new_stop_loss 为新的止损价
    """
    if already_applied:
        return BreakevenResult(should_apply=False)

    threshold = atr_at_entry * breakeven_atr_threshold

    if action == "buy" and current_price >= entry_price + threshold:
        return BreakevenResult(should_apply=True, new_stop_loss=entry_price + 0.01)
    elif action == "sell" and current_price <= entry_price - threshold:
        return BreakevenResult(should_apply=True, new_stop_loss=entry_price - 0.01)

    return BreakevenResult(should_apply=False)


def check_trailing_stop(
    action: str,
    current_stop_loss: float,
    atr_at_entry: float,
    trailing_atr_multiplier: float,
    breakeven_applied: bool,
    highest_price: Optional[float],
    lowest_price: Optional[float],
) -> TrailingStopResult:
    """判断是否应移动跟踪止损。

    前提：breakeven 已应用。跟踪距离 = atr_at_entry * trailing_atr_multiplier。

    Args:
        action: "buy" 或 "sell"
        current_stop_loss: 当前止损价
        atr_at_entry: 入场时的 ATR 值
        trailing_atr_multiplier: ATR 倍数
        breakeven_applied: 是否已应用 breakeven（未应用则不触发 trailing）
        highest_price: 持仓期间最高价（多头用）
        lowest_price: 持仓期间最低价（空头用）

    Returns:
        TrailingStopResult，should_update=True 时 new_stop_loss 为新的止损价
    """
    if not breakeven_applied:
        return TrailingStopResult(should_update=False)

    trail_distance = atr_at_entry * trailing_atr_multiplier

    if action == "buy" and highest_price is not None:
        new_sl = highest_price - trail_distance
        if new_sl > current_stop_loss:
            return TrailingStopResult(should_update=True, new_stop_loss=new_sl)

    elif action == "sell" and lowest_price is not None:
        new_sl = lowest_price + trail_distance
        if new_sl < current_stop_loss:
            return TrailingStopResult(should_update=True, new_stop_loss=new_sl)

    return TrailingStopResult(should_update=False)


@dataclass
class TrailingTakeProfitResult:
    """Trailing Take Profit 判定结果。"""

    should_update: bool
    new_take_profit: Optional[float] = None


def check_trailing_take_profit(
    action: str,
    entry_price: float,
    current_take_profit: float,
    atr_at_entry: float,
    activation_atr: float,
    trail_atr: float,
    highest_price: Optional[float],
    lowest_price: Optional[float],
) -> TrailingTakeProfitResult:
    """判断是否应收缩止盈（Trailing TP）。

    当浮盈超过 activation_atr × ATR 后启动，TP 缩小到
    最高盈利点 - trail_atr × ATR（多头）。
    TP 只收缩不放大。

    Args:
        action: "buy" 或 "sell"
        entry_price: 入场价
        current_take_profit: 当前止盈价
        atr_at_entry: 入场时的 ATR 值
        activation_atr: 激活 trailing TP 的最小浮盈（ATR 倍数）
        trail_atr: 从最高盈利点回撤的距离（ATR 倍数）
        highest_price: 持仓期间最高价（多头用）
        lowest_price: 持仓期间最低价（空头用）

    Returns:
        TrailingTakeProfitResult，should_update=True 时 new_take_profit 为新的止盈价
    """
    activation_distance = atr_at_entry * activation_atr
    trail_distance = atr_at_entry * trail_atr

    if action == "buy" and highest_price is not None:
        profit = highest_price - entry_price
        if profit >= activation_distance:
            # TP 收缩到：最高价 - trailing 距离
            new_tp = highest_price - trail_distance
            # 确保 new_tp 仍在入场价之上（至少保本）
            if new_tp <= entry_price:
                return TrailingTakeProfitResult(should_update=False)
            # TP 只收缩（对多头：新 TP < 当前 TP）
            if new_tp < current_take_profit:
                return TrailingTakeProfitResult(should_update=True, new_take_profit=new_tp)

    elif action == "sell" and lowest_price is not None:
        profit = entry_price - lowest_price
        if profit >= activation_distance:
            new_tp = lowest_price + trail_distance
            if new_tp >= entry_price:
                return TrailingTakeProfitResult(should_update=False)
            if new_tp > current_take_profit:
                return TrailingTakeProfitResult(should_update=True, new_take_profit=new_tp)

    return TrailingTakeProfitResult(should_update=False)


def should_close_end_of_day(
    current_time: datetime,
    close_hour_utc: int,
    close_minute_utc: int,
    last_close_date: Optional[str],
) -> EndOfDayResult:
    """判断是否到达日终平仓时间。

    Args:
        current_time: 当前时间（必须有时区信息，或视为 UTC）
        close_hour_utc: 平仓时间（UTC 小时）
        close_minute_utc: 平仓时间（UTC 分钟）
        last_close_date: 上次执行平仓的日期字符串（ISO 格式），防止同日重复

    Returns:
        EndOfDayResult，should_close=True 时应执行全部平仓
    """
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)

    closeout_time = current_time.replace(
        hour=close_hour_utc,
        minute=close_minute_utc,
        second=0,
        microsecond=0,
    )
    if current_time < closeout_time:
        return EndOfDayResult(should_close=False)

    day_key = current_time.date().isoformat()
    if last_close_date == day_key:
        return EndOfDayResult(should_close=False)

    return EndOfDayResult(should_close=True)


# ═══════════════════════════════════════════════════════════════════════════════
# 指标驱动的动态出场（Indicator-Driven Exit）
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class IndicatorExitConfig:
    """指标驱动出场配置——所有参数可通过 backtest.ini 配置和优化器搜索。"""

    enabled: bool = False

    # ── SuperTrend 方向翻转 ──
    supertrend_enabled: bool = True
    supertrend_tighten_atr: float = 0.5

    # ── RSI 极端区域反转 ──
    rsi_enabled: bool = True
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0
    rsi_delta_threshold: float = 5.0
    rsi_tighten_atr: float = 0.5

    # ── MACD 柱状图符号翻转 ──
    macd_enabled: bool = True
    macd_tighten_atr: float = 0.5

    # ── ADX 趋势强度衰减 ──
    adx_enabled: bool = True
    adx_entry_min: float = 25.0
    adx_collapse_threshold: float = 10.0
    adx_tighten_atr: float = 0.3


@dataclass
class IndicatorExitResult:
    """指标驱动出场判定结果。"""

    should_tighten_sl: bool = False
    should_close: bool = False
    new_stop_loss: Optional[float] = None
    reason: str = ""


def _compute_tightened_sl(
    direction: str,
    current_price: float,
    current_sl: float,
    atr_at_entry: float,
    tighten_atr: float,
) -> Optional[float]:
    """计算收紧后的 SL，只收紧不放松。返回 None 表示无需更新。"""
    if direction == "buy":
        new_sl = current_price - tighten_atr * atr_at_entry
        return new_sl if new_sl > current_sl else None
    else:
        new_sl = current_price + tighten_atr * atr_at_entry
        return new_sl if new_sl < current_sl else None


def check_supertrend_reversal(
    direction: str,
    entry_st_direction: float,
    current_st_direction: float,
    current_price: float,
    current_sl: float,
    atr_at_entry: float,
    tighten_atr: float,
) -> IndicatorExitResult:
    """SuperTrend 方向翻转检测。

    当 SuperTrend direction 从与持仓同向翻转为反向时，趋势论点失效，收紧 SL。
    """
    # buy 持仓需要 ST direction > 0（上行）；sell 需要 < 0
    if direction == "buy" and entry_st_direction > 0 and current_st_direction < 0:
        new_sl = _compute_tightened_sl(direction, current_price, current_sl, atr_at_entry, tighten_atr)
        if new_sl is not None:
            return IndicatorExitResult(should_tighten_sl=True, new_stop_loss=new_sl, reason="supertrend")
    elif direction == "sell" and entry_st_direction < 0 and current_st_direction > 0:
        new_sl = _compute_tightened_sl(direction, current_price, current_sl, atr_at_entry, tighten_atr)
        if new_sl is not None:
            return IndicatorExitResult(should_tighten_sl=True, new_stop_loss=new_sl, reason="supertrend")
    return IndicatorExitResult()


def check_rsi_reversal(
    direction: str,
    rsi: float,
    rsi_d3: float,
    overbought: float,
    oversold: float,
    delta_threshold: float,
    current_price: float,
    current_sl: float,
    atr_at_entry: float,
    tighten_atr: float,
) -> IndicatorExitResult:
    """RSI 极端区域反转检测。

    多头：RSI > overbought 且 rsi_d3 < -delta_threshold（见顶回落）→ 收紧 SL
    空头：RSI < oversold 且 rsi_d3 > delta_threshold（触底反弹）→ 收紧 SL
    """
    if direction == "buy" and rsi > overbought and rsi_d3 < -delta_threshold:
        new_sl = _compute_tightened_sl(direction, current_price, current_sl, atr_at_entry, tighten_atr)
        if new_sl is not None:
            return IndicatorExitResult(should_tighten_sl=True, new_stop_loss=new_sl, reason="rsi")
    elif direction == "sell" and rsi < oversold and rsi_d3 > delta_threshold:
        new_sl = _compute_tightened_sl(direction, current_price, current_sl, atr_at_entry, tighten_atr)
        if new_sl is not None:
            return IndicatorExitResult(should_tighten_sl=True, new_stop_loss=new_sl, reason="rsi")
    return IndicatorExitResult()


def check_macd_reversal(
    direction: str,
    entry_hist: float,
    current_hist: float,
    current_price: float,
    current_sl: float,
    atr_at_entry: float,
    tighten_atr: float,
) -> IndicatorExitResult:
    """MACD 柱状图符号翻转检测。

    多头：入场 histogram > 0，当前 < 0（动量转空）→ 收紧 SL
    空头：入场 histogram < 0，当前 > 0（动量转多）→ 收紧 SL
    """
    if direction == "buy" and entry_hist > 0 and current_hist < 0:
        new_sl = _compute_tightened_sl(direction, current_price, current_sl, atr_at_entry, tighten_atr)
        if new_sl is not None:
            return IndicatorExitResult(should_tighten_sl=True, new_stop_loss=new_sl, reason="macd")
    elif direction == "sell" and entry_hist < 0 and current_hist > 0:
        new_sl = _compute_tightened_sl(direction, current_price, current_sl, atr_at_entry, tighten_atr)
        if new_sl is not None:
            return IndicatorExitResult(should_tighten_sl=True, new_stop_loss=new_sl, reason="macd")
    return IndicatorExitResult()


def check_adx_collapse(
    direction: str,
    entry_adx: float,
    current_adx: float,
    adx_entry_min: float,
    collapse_threshold: float,
    current_price: float,
    current_sl: float,
    atr_at_entry: float,
    tighten_atr: float,
) -> IndicatorExitResult:
    """ADX 趋势强度衰减检测。

    入场时 ADX >= adx_entry_min（有趋势），当前 ADX 下跌超 collapse_threshold → 收紧 SL。
    """
    if entry_adx >= adx_entry_min and (entry_adx - current_adx) >= collapse_threshold:
        new_sl = _compute_tightened_sl(direction, current_price, current_sl, atr_at_entry, tighten_atr)
        if new_sl is not None:
            return IndicatorExitResult(should_tighten_sl=True, new_stop_loss=new_sl, reason="adx")
    return IndicatorExitResult()


def check_indicator_exit(
    direction: str,
    current_price: float,
    current_sl: float,
    entry_price: float,
    atr_at_entry: float,
    entry_indicators: Dict[str, Dict[str, Any]],
    current_indicators: Dict[str, Dict[str, Any]],
    config: IndicatorExitConfig,
) -> IndicatorExitResult:
    """指标驱动出场组合检测入口。

    按优先级调用所有启用的子检测，返回最激进的结果（最紧的 SL）。
    从 indicators Dict 中安全提取值，缺失时跳过对应检测。
    """
    if not config.enabled:
        return IndicatorExitResult()

    best = IndicatorExitResult()

    def _pick_tighter(candidate: IndicatorExitResult) -> None:
        nonlocal best
        if not candidate.should_tighten_sl and not candidate.should_close:
            return
        if candidate.should_close:
            best = candidate
            return
        if not best.should_tighten_sl:
            best = candidate
            return
        # 比较谁的 SL 更紧
        if candidate.new_stop_loss is not None and best.new_stop_loss is not None:
            if direction == "buy" and candidate.new_stop_loss > best.new_stop_loss:
                best = candidate
            elif direction == "sell" and candidate.new_stop_loss < best.new_stop_loss:
                best = candidate

    # P0: SuperTrend 方向翻转
    if config.supertrend_enabled:
        entry_st = entry_indicators.get("supertrend14", {}).get("direction")
        current_st = current_indicators.get("supertrend14", {}).get("direction")
        if entry_st is not None and current_st is not None:
            _pick_tighter(check_supertrend_reversal(
                direction, entry_st, current_st,
                current_price, current_sl, atr_at_entry, config.supertrend_tighten_atr,
            ))

    # P1: RSI 极值反转
    if config.rsi_enabled:
        rsi = current_indicators.get("rsi14", {}).get("rsi")
        rsi_d3 = current_indicators.get("rsi14", {}).get("rsi_d3")
        if rsi is not None and rsi_d3 is not None:
            _pick_tighter(check_rsi_reversal(
                direction, rsi, rsi_d3,
                config.rsi_overbought, config.rsi_oversold, config.rsi_delta_threshold,
                current_price, current_sl, atr_at_entry, config.rsi_tighten_atr,
            ))

    # P2: MACD histogram 翻转
    if config.macd_enabled:
        entry_hist = entry_indicators.get("macd", {}).get("hist")
        current_hist = current_indicators.get("macd", {}).get("hist")
        if entry_hist is not None and current_hist is not None:
            _pick_tighter(check_macd_reversal(
                direction, entry_hist, current_hist,
                current_price, current_sl, atr_at_entry, config.macd_tighten_atr,
            ))

    # P3: ADX 趋势强度衰减
    if config.adx_enabled:
        entry_adx = entry_indicators.get("adx14", {}).get("adx")
        current_adx = current_indicators.get("adx14", {}).get("adx")
        if entry_adx is not None and current_adx is not None:
            _pick_tighter(check_adx_collapse(
                direction, entry_adx, current_adx,
                config.adx_entry_min, config.adx_collapse_threshold,
                current_price, current_sl, atr_at_entry, config.adx_tighten_atr,
            ))

    return best
