"""统一出场规则系统。

所有平仓决策的纯函数集合，回测和实盘共用同一套代码。

6 条独立规则：

  规则 1: 初始 SL — 固定风险底线（R = |entry - initial_sl|）
  规则 2: Chandelier Trailing — current_ATR 动态跟踪 + Breakeven + 梯度锁利
  规则 3: 信号反转 — N-bar 连续确认后主动退出
  规则 4: 超时 — 持仓未达 breakeven 超过 N bars 退出
  规则 5: 硬上界 TP — R 倍数天花板
  规则 6: 日终平仓 — 到达指定 UTC 时间全平

Regime-aware: 根据 (strategy_category, current_regime) 通过 aggression 系数
动态选择出场 profile，实现"顺势仓位宽 trail，逆势仓位紧 trail"。

Per-TF 缩放: chandelier_multiplier 按时间框架缩放，补偿不同 TF 的 ATR 统计特性差异。

设计原则：
  - 纯函数，无状态，不依赖任何运行时组件
  - 回测 PortfolioTracker 和实盘 PositionManager 共用
  - 规则之间不耦合——每条规则独立判断，调用方按顺序执行
  - SL 只升不降（多头）/ 只降不升（空头）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..reasons import (
    REASON_SIGNAL_EXIT,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    REASON_TIMEOUT,
    REASON_TRAILING_STOP,
)

# ── 配置 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExitProfile:
    """单个出场参数集。由 aggression 系数驱动，三参数内在关联。

    aggression 越高 → 越"放手"：宽 trail、低锁利、晚 breakeven（顺势）
    aggression 越低 → 越"保护"：紧 trail、高锁利、早 breakeven（逆势）
    """

    chandelier_atr_multiplier: float = 2.5
    lock_ratio: float = 0.5
    breakeven_r: float = 1.0


def profile_from_aggression(alpha: float) -> ExitProfile:
    """从 aggression 系数 α ∈ [0, 1] 生成出场 profile。

    三参数公式（α 驱动，内在一致）：
      chandelier_atr_multiplier = 1.5 + 2.0 × α    → [1.5, 3.5]
      lock_ratio                = 0.9 - 0.5 × α    → [0.4, 0.9]
      breakeven_r               = 0.6 + 0.6 × α    → [0.6, 1.2]
    """
    alpha = max(0.0, min(1.0, alpha))
    return ExitProfile(
        chandelier_atr_multiplier=round(1.5 + 2.0 * alpha, 4),
        lock_ratio=round(0.9 - 0.5 * alpha, 4),
        breakeven_r=round(0.6 + 0.6 * alpha, 4),
    )


# ── Regime × Category → aggression 映射 ──────────────────────────────────
# 判定逻辑：category 与 regime 是否"顺势"
#   顺势 = (trend+trending) / (reversion+ranging) / (breakout+breakout|trending)
#   逆势 = (trend+ranging) / (reversion+trending|breakout) / (breakout+ranging)
#   中性 = uncertain 或其他

# 默认 aggression 系数表（可被 INI 覆盖）
DEFAULT_AGGRESSION_MAP: Dict[Tuple[str, str], float] = {
    # trend 策略
    ("trend", "trending"):   0.85,   # 顺势：宽 trail
    ("trend", "ranging"):    0.15,   # 逆势：紧 trail
    ("trend", "breakout"):   0.65,   # 偏顺势
    ("trend", "uncertain"):  0.50,   # 中性
    # reversion 策略
    ("reversion", "trending"):  0.10,   # 逆势：最紧
    ("reversion", "ranging"):   0.80,   # 顺势：宽 trail
    ("reversion", "breakout"):  0.10,   # 逆势
    ("reversion", "uncertain"): 0.45,   # 偏逆势
    # breakout 策略
    ("breakout", "trending"):   0.85,   # 突破进入趋势
    ("breakout", "ranging"):    0.10,   # 假突破：快退
    ("breakout", "breakout"):   0.85,   # 顺势
    ("breakout", "uncertain"):  0.50,   # 中性
    # session breakout 策略（类似 breakout，但 ranging 中略宽容）
    ("session", "trending"):    0.80,
    ("session", "ranging"):     0.20,
    ("session", "breakout"):    0.80,
    ("session", "uncertain"):   0.50,
    # multi_tf (趋势延续，类似 trend)
    ("multi_tf", "trending"):   0.85,
    ("multi_tf", "ranging"):    0.15,
    ("multi_tf", "breakout"):   0.65,
    ("multi_tf", "uncertain"):  0.50,
    # price_action (sweep/反转，类似 reversion 偏保护)
    ("price_action", "trending"):  0.15,
    ("price_action", "ranging"):   0.70,
    ("price_action", "breakout"):  0.15,
    ("price_action", "uncertain"): 0.45,
}

DEFAULT_AGGRESSION = 0.50

# ── Per-TF trail 缩放因子 ─────────────────────────────────────────────────
# 补偿不同 TF 的 ATR 统计特性差异：
#   低 TF (M5/M15): ATR 自相关低、尾部风险高 → 需更宽 trail
#   高 TF (H4/D1): ATR 平滑、可预测性高 → 可略紧

DEFAULT_TF_TRAIL_SCALE: Dict[str, float] = {
    "M5":  1.20,
    "M15": 1.10,
    "M30": 1.00,
    "H1":  1.00,
    "H4":  0.95,
    "D1":  0.90,
}


def resolve_exit_profile(
    strategy_category: str,
    current_regime: str,
    aggression_overrides: Optional[Dict[Tuple[str, str], float]] = None,
) -> ExitProfile:
    """根据 (strategy_category, current_regime) 生成出场 profile。

    查找优先级：aggression_overrides → DEFAULT_AGGRESSION_MAP → DEFAULT_AGGRESSION
    """
    key = (strategy_category.lower(), current_regime.lower())
    source = aggression_overrides if aggression_overrides else DEFAULT_AGGRESSION_MAP
    alpha = source.get(key)
    if alpha is None:
        alpha = DEFAULT_AGGRESSION_MAP.get(key, DEFAULT_AGGRESSION)
    return profile_from_aggression(alpha)


def resolve_aggression(
    strategy_category: str,
    current_regime: str,
    aggression_overrides: Optional[Dict[Tuple[str, str], float]] = None,
) -> float:
    """返回 (strategy_category, current_regime) 对应的 aggression 系数。

    与 resolve_exit_profile 逻辑一致，但只返回 α 值而非完整 profile。
    用于 regime 切换时比较 aggression 变化。
    """
    key = (strategy_category.lower(), current_regime.lower())
    source = aggression_overrides if aggression_overrides else DEFAULT_AGGRESSION_MAP
    alpha = source.get(key)
    if alpha is None:
        alpha = DEFAULT_AGGRESSION_MAP.get(key, DEFAULT_AGGRESSION)
    return alpha


def resolve_tf_trail_scale(
    timeframe: str,
    tf_scale_overrides: Optional[Dict[str, float]] = None,
) -> float:
    """返回 per-TF 的 Chandelier trail 缩放因子。"""
    tf = timeframe.strip().upper() if timeframe else ""
    if tf_scale_overrides and tf in tf_scale_overrides:
        return tf_scale_overrides[tf]
    return DEFAULT_TF_TRAIL_SCALE.get(tf, 1.0)


@dataclass(frozen=True)
class ChandelierConfig:
    """Chandelier Exit 出场配置。全部参数从 INI 注入。"""

    # 规则 2b: Breakeven + 连续锁利基础参数
    breakeven_enabled: bool = True
    breakeven_buffer_r: float = 0.1
    min_breakeven_buffer: float = 1.0

    # 规则 3: 信号反转
    signal_exit_enabled: bool = True
    signal_exit_confirmation_bars: int = 2

    # 规则 4: 超时
    timeout_bars: int = 0  # 0=禁用

    # 硬上界 TP
    max_tp_r: float = 5.0

    # 是否启用 regime-aware profile（True=按 aggression 查找，False=使用默认 profile）
    regime_aware: bool = True
    default_profile: ExitProfile = field(default_factory=lambda: ExitProfile())

    # Aggression 系数覆盖：(category, regime) → α ∈ [0, 1]
    aggression_overrides: Dict[Tuple[str, str], float] = field(default_factory=dict)

    # Per-TF trail 缩放因子
    tf_trail_scale: Dict[str, float] = field(default_factory=dict)

    # R 单位保护：effective_chandelier_mult ≥ initial_sl_atr_mult
    # 防止 Chandelier 在入场时立即收紧 SL，导致 R 单位失真
    enforce_r_floor: bool = True


# ── 结果 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExitCheckResult:
    """单条规则的检查结果。"""

    should_close: bool
    close_reason: str  # "stop_loss" / "take_profit" / "trailing_stop" / "signal_exit" / "timeout" / ""
    new_stop_loss: Optional[float]  # 建议 SL（None = 不变）
    r_multiple: float  # 当前浮盈 R 倍数
    breakeven_activated: bool


# ── 规则 1: 初始 SL 触发 ────────────────────────────────────────────────────


def check_initial_sl(
    action: str,
    bar_high: float,
    bar_low: float,
    current_stop_loss: float,
    r_multiple: float,
) -> Optional[ExitCheckResult]:
    """检查初始 SL 是否被触发。触发则返回结果，否则 None。"""
    if action == "buy" and bar_low <= current_stop_loss:
        return ExitCheckResult(
            should_close=True,
            close_reason=REASON_STOP_LOSS,
            new_stop_loss=None,
            r_multiple=r_multiple,
            breakeven_activated=False,
        )
    if action == "sell" and bar_high >= current_stop_loss:
        return ExitCheckResult(
            should_close=True,
            close_reason=REASON_STOP_LOSS,
            new_stop_loss=None,
            r_multiple=r_multiple,
            breakeven_activated=False,
        )
    return None


# ── 规则 1b: 硬上界 TP ─────────────────────────────────────────────────────


def check_hard_tp(
    action: str,
    entry_price: float,
    bar_high: float,
    bar_low: float,
    initial_risk: float,
    max_tp_r: float,
) -> Optional[ExitCheckResult]:
    """检查硬上界 TP 是否到达。到达则返回结果，否则 None。"""
    if initial_risk <= 0 or max_tp_r <= 0:
        return None

    tp_distance = max_tp_r * initial_risk
    if action == "buy":
        if bar_high >= entry_price + tp_distance:
            return ExitCheckResult(
                should_close=True,
                close_reason=REASON_TAKE_PROFIT,
                new_stop_loss=None,
                r_multiple=max_tp_r,
                breakeven_activated=True,
            )
    else:
        if bar_low <= entry_price - tp_distance:
            return ExitCheckResult(
                should_close=True,
                close_reason=REASON_TAKE_PROFIT,
                new_stop_loss=None,
                r_multiple=max_tp_r,
                breakeven_activated=True,
            )
    return None


# ── 规则 2: Chandelier Trailing SL ─────────────────────────────────────────


def compute_chandelier_sl(
    action: str,
    peak_price: float,
    current_atr: float,
    chandelier_multiplier: float,
) -> Optional[float]:
    """计算 Chandelier trailing SL 位置。

    trail_sl = peak_price - current_ATR × multiplier（多头）
    trail_sl = peak_price + current_ATR × multiplier（空头）

    用 current_ATR（非入场 ATR），天然随波动率呼吸。
    """
    if current_atr <= 0 or chandelier_multiplier <= 0:
        return None
    trail_distance = current_atr * chandelier_multiplier
    if action == "buy":
        return peak_price - trail_distance
    else:
        return peak_price + trail_distance


# ── 规则 2b: Breakeven 保护 ─────────────────────────────────────────────────


def compute_breakeven_sl(
    action: str,
    entry_price: float,
    peak_price: float,
    initial_risk: float,
    breakeven_r: float,
    breakeven_buffer_r: float,
    min_buffer: float,
    already_activated: bool,
    lock_ratio: float = 0.5,
) -> tuple[Optional[float], bool]:
    """计算 breakeven + 连续锁利 SL 位置。

    当浮盈达到 breakeven_r 后，SL 移到保本位。
    之后随浮盈增长，连续锁定利润（按 lock_ratio 比例）。

    例：breakeven_r=1.0, lock_ratio=0.5
      浮盈 1.0R → SL = entry + 0.1R（保本 buffer）
      浮盈 1.5R → SL = entry + 0.75R（锁 50% of 1.5R）
      浮盈 2.0R → SL = entry + 1.0R（锁 50% of 2.0R）
      浮盈 3.0R → SL = entry + 1.5R（锁 50% of 3.0R）

    Returns:
        (lock_sl, is_activated)
        lock_sl: None 表示未激活
    """
    if initial_risk <= 0:
        return None, already_activated

    # 计算历史最高 R 倍数
    if action == "buy":
        peak_profit_r = (peak_price - entry_price) / initial_risk
    else:
        peak_profit_r = (entry_price - peak_price) / initial_risk

    activated = already_activated or peak_profit_r >= breakeven_r

    if not activated:
        return None, False

    # 基础保本 buffer
    buffer = max(breakeven_buffer_r * initial_risk, min_buffer)

    # 连续锁利：锁定 = 峰值浮盈 × lock_ratio（线性跟踪，无阶梯跳变）
    if lock_ratio > 0 and peak_profit_r > breakeven_r:
        lock_amount = peak_profit_r * lock_ratio * initial_risk
        lock_amount = max(lock_amount, buffer)
    else:
        lock_amount = buffer

    if action == "buy":
        return entry_price + lock_amount, True
    else:
        return entry_price - lock_amount, True


# ── 规则 2 合并: SL 取最紧值 ────────────────────────────────────────────────


def merge_trailing_sl(
    action: str,
    current_stop_loss: float,
    chandelier_sl: Optional[float],
    breakeven_sl: Optional[float],
) -> Optional[float]:
    """合并 Chandelier + Breakeven SL，取最紧且只升不降。

    Returns:
        new_sl: 比 current_stop_loss 更紧时返回，否则 None
    """
    candidates = [current_stop_loss]
    if chandelier_sl is not None:
        candidates.append(chandelier_sl)
    if breakeven_sl is not None:
        candidates.append(breakeven_sl)

    if action == "buy":
        best = max(candidates)
        return round(best, 2) if best > current_stop_loss else None
    else:
        best = min(candidates)
        return round(best, 2) if best < current_stop_loss else None


# ── 规则 3: 信号反转 N-bar 确认 ─────────────────────────────────────────────


def check_signal_reversal(
    action: str,
    recent_signal_dirs: List[str],
    confirmation_bars: int,
) -> bool:
    """检查最近 N 根 bar 是否连续信号反转。

    Args:
        action: 持仓方向 "buy" / "sell"
        recent_signal_dirs: 最近的策略/投票方向列表（最新在末尾）
        confirmation_bars: 需要连续几根 bar 方向相反

    Returns:
        True = 确认反转，应退出
    """
    if not recent_signal_dirs or len(recent_signal_dirs) < confirmation_bars:
        return False

    opposite = "sell" if action == "buy" else "buy"
    recent = recent_signal_dirs[-confirmation_bars:]
    return all(d == opposite for d in recent)


# ── 规则 4: 超时退出 ────────────────────────────────────────────────────────


def check_timeout(
    action: str,
    entry_price: float,
    bar_price: float,
    initial_risk: float,
    bars_held: int,
    timeout_bars: int,
    breakeven_r: float,
) -> bool:
    """检查是否超时（持仓太久未达 breakeven）。

    仅在未盈利时触发——已盈利的仓位由 Chandelier 管理。

    Returns:
        True = 超时，应退出
    """
    if timeout_bars <= 0 or bars_held < timeout_bars:
        return False
    if initial_risk <= 0:
        return False

    if action == "buy":
        profit_r = (bar_price - entry_price) / initial_risk
    else:
        profit_r = (entry_price - bar_price) / initial_risk

    return profit_r < breakeven_r


# ── 统一入口 ────────────────────────────────────────────────────────────────


def evaluate_exit(
    action: str,
    entry_price: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    current_stop_loss: float,
    initial_risk: float,
    peak_price: float,
    current_atr: float,
    *,
    bars_held: int = 0,
    breakeven_already_activated: bool = False,
    recent_signal_dirs: Optional[List[str]] = None,
    strategy_category: str = "",
    current_regime: str = "",
    timeframe: str = "",
    initial_sl_atr_mult: float = 0.0,
    config: Optional[ChandelierConfig] = None,
    exit_spec: Optional[Dict[str, Any]] = None,
) -> ExitCheckResult:
    """统一出场评估入口。按顺序执行 6 条规则。

    出场 profile 来源（优先级从高到低）：
      1. exit_spec["aggression"]（策略 _exit_spec() 输出）
      2. (strategy_category, current_regime) 映射（配置缺失时使用默认 α）
      3. default_profile（全局默认 α=0.50）
    """
    if config is None:
        config = ChandelierConfig()

    # 策略 exit_spec 优先
    if exit_spec and "aggression" in exit_spec:
        profile = profile_from_aggression(exit_spec["aggression"])
    elif config.regime_aware and strategy_category and current_regime:
        profile = resolve_exit_profile(
            strategy_category, current_regime, config.aggression_overrides or None,
        )
    else:
        profile = config.default_profile

    # ── Per-TF trail 缩放 + R 单位保护 ──
    raw_chandelier_mult = profile.chandelier_atr_multiplier
    tf_scale = resolve_tf_trail_scale(timeframe, config.tf_trail_scale or None)
    effective_chandelier_mult = raw_chandelier_mult * tf_scale

    # R 单位保护：Chandelier mult 不得低于入场 SL mult，否则 R 单位失真
    if config.enforce_r_floor and initial_sl_atr_mult > 0:
        effective_chandelier_mult = max(effective_chandelier_mult, initial_sl_atr_mult)

    # 计算 R 倍数（用 bar_close 近似）
    r_multiple = 0.0
    if initial_risk > 0:
        if action == "buy":
            r_multiple = (bar_close - entry_price) / initial_risk
        else:
            r_multiple = (entry_price - bar_close) / initial_risk

    # ① 初始 SL 触发
    result = check_initial_sl(action, bar_high, bar_low, current_stop_loss, r_multiple)
    if result is not None:
        return result

    # ② 硬上界 TP
    result = check_hard_tp(
        action, entry_price, bar_high, bar_low, initial_risk, config.max_tp_r,
    )
    if result is not None:
        return result

    # ③ Chandelier trailing + Breakeven（参数来自 regime-aware profile + TF 缩放）
    chandelier_sl = compute_chandelier_sl(
        action, peak_price, current_atr, effective_chandelier_mult,
    )

    breakeven_sl, be_activated = compute_breakeven_sl(
        action, entry_price, peak_price, initial_risk,
        profile.breakeven_r, config.breakeven_buffer_r,
        config.min_breakeven_buffer, breakeven_already_activated,
        lock_ratio=profile.lock_ratio,
    )

    new_sl = merge_trailing_sl(action, current_stop_loss, chandelier_sl, breakeven_sl)

    # 合并后 SL 是否被当前 bar 触发？
    check_sl = new_sl if new_sl is not None else current_stop_loss
    if action == "buy" and bar_low <= check_sl:
        return ExitCheckResult(
            should_close=True,
            close_reason=REASON_TRAILING_STOP,
            new_stop_loss=new_sl,
            r_multiple=round(r_multiple, 4),
            breakeven_activated=be_activated,
        )
    if action == "sell" and bar_high >= check_sl:
        return ExitCheckResult(
            should_close=True,
            close_reason=REASON_TRAILING_STOP,
            new_stop_loss=new_sl,
            r_multiple=round(r_multiple, 4),
            breakeven_activated=be_activated,
        )

    # ④ 信号反转
    if config.signal_exit_enabled and recent_signal_dirs:
        if check_signal_reversal(action, recent_signal_dirs, config.signal_exit_confirmation_bars):
            return ExitCheckResult(
                should_close=True,
                close_reason=REASON_SIGNAL_EXIT,
                new_stop_loss=new_sl,
                r_multiple=round(r_multiple, 4),
                breakeven_activated=be_activated,
            )

    # ⑤ 超时
    if config.timeout_bars > 0:
        if check_timeout(
            action, entry_price, bar_close, initial_risk,
            bars_held, config.timeout_bars, profile.breakeven_r,
        ):
            return ExitCheckResult(
                should_close=True,
                close_reason=REASON_TIMEOUT,
                new_stop_loss=new_sl,
                r_multiple=round(r_multiple, 4),
                breakeven_activated=be_activated,
            )

    # ⑥ 无出场信号
    return ExitCheckResult(
        should_close=False,
        close_reason="",
        new_stop_loss=new_sl,
        r_multiple=round(r_multiple, 4),
        breakeven_activated=be_activated,
    )


# ── 规则 6: 日终平仓 ────────────────────────────────────────────────────────


def check_end_of_day(
    current_time: datetime,
    close_hour_utc: int,
    close_minute_utc: int,
    last_close_date: Optional[str],
) -> bool:
    """检查是否到达日终平仓时间。

    Returns:
        True = 应平仓所有持仓
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
        return False

    day_key = current_time.date().isoformat()
    if last_close_date == day_key:
        return False

    return True
