"""统一出场规则系统。

所有平仓决策的纯函数集合，回测和实盘共用同一套代码。

6 条独立规则：

  规则 1: 初始 SL — 固定风险底线（R = |entry - initial_sl|）
  规则 2: Chandelier Trailing — current_ATR 动态跟踪 + Breakeven + 梯度锁利
  规则 3: 信号反转 — N-bar 连续确认后主动退出
  规则 4: 超时 — 持仓未达 breakeven 超过 N bars 退出
  规则 5: 硬上界 TP — R 倍数天花板
  规则 6: 日终平仓 — 到达指定 UTC 时间全平

Regime-aware: 根据 (strategy_category, current_regime) 动态选择出场 profile，
实现"顺势仓位宽 trail，逆势仓位紧 trail"。

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


# ── 配置 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExitProfile:
    """单个出场参数集。可按 (strategy_category, regime) 组合配置。"""

    chandelier_atr_multiplier: float = 2.5
    lock_ratio: float = 0.5
    breakeven_r: float = 1.0


# ── Regime × Category 出场 profile 矩阵 ────────────────────────────────────
# 查找优先级：(category, regime) → (category, "default") → DEFAULT_PROFILE
# 顺势仓位：宽 trail + 低锁利（让利润跑）
# 逆势仓位：窄 trail + 高锁利（快速保护）

DEFAULT_PROFILE = ExitProfile(chandelier_atr_multiplier=2.5, lock_ratio=0.5, breakeven_r=1.0)

EXIT_PROFILE_MATRIX: Dict[Tuple[str, str], ExitProfile] = {
    # trend 策略
    ("trend", "trending"):   ExitProfile(3.5, 0.4, 1.2),  # 顺势：宽 trail
    ("trend", "ranging"):    ExitProfile(2.0, 0.8, 0.8),  # 逆势：紧 trail
    ("trend", "breakout"):   ExitProfile(3.0, 0.5, 1.0),
    ("trend", "uncertain"):  ExitProfile(2.5, 0.6, 0.9),
    # reversion 策略
    ("reversion", "trending"):  ExitProfile(1.5, 0.9, 0.6),  # 逆势：最紧
    ("reversion", "ranging"):   ExitProfile(3.0, 0.5, 1.0),  # 顺势：宽 trail
    ("reversion", "breakout"):  ExitProfile(1.5, 0.9, 0.6),  # 逆势
    ("reversion", "uncertain"): ExitProfile(2.0, 0.7, 0.8),
    # breakout 策略
    ("breakout", "trending"):   ExitProfile(3.5, 0.4, 1.2),  # 突破进入趋势
    ("breakout", "ranging"):    ExitProfile(1.5, 0.9, 0.6),  # 假突破：快退
    ("breakout", "breakout"):   ExitProfile(3.5, 0.4, 1.2),  # 顺势
    ("breakout", "uncertain"):  ExitProfile(2.5, 0.6, 0.9),
}


def resolve_exit_profile(
    strategy_category: str,
    current_regime: str,
) -> ExitProfile:
    """根据 (strategy_category, current_regime) 查找出场 profile。"""
    key = (strategy_category.lower(), current_regime.lower())
    profile = EXIT_PROFILE_MATRIX.get(key)
    if profile is not None:
        return profile
    # fallback: 只按 category 查默认
    default_key = (strategy_category.lower(), "uncertain")
    return EXIT_PROFILE_MATRIX.get(default_key, DEFAULT_PROFILE)


@dataclass(frozen=True)
class ChandelierConfig:
    """Chandelier Exit 出场配置。"""

    # 规则 2b: Breakeven + 梯度锁利基础参数
    breakeven_enabled: bool = True
    breakeven_buffer_r: float = 0.1
    min_breakeven_buffer: float = 1.0
    lock_step_r: float = 0.5

    # 规则 3: 信号反转
    signal_exit_enabled: bool = True
    signal_exit_confirmation_bars: int = 2

    # 规则 4: 超时
    timeout_bars: int = 0  # 0=禁用

    # 硬上界 TP
    max_tp_r: float = 5.0

    # 是否启用 regime-aware profile（True=按矩阵查找，False=用 fallback_profile）
    regime_aware: bool = True
    fallback_profile: ExitProfile = field(default_factory=lambda: DEFAULT_PROFILE)


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
            close_reason="stop_loss",
            new_stop_loss=None,
            r_multiple=r_multiple,
            breakeven_activated=False,
        )
    if action == "sell" and bar_high >= current_stop_loss:
        return ExitCheckResult(
            should_close=True,
            close_reason="stop_loss",
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
                close_reason="take_profit",
                new_stop_loss=None,
                r_multiple=max_tp_r,
                breakeven_activated=True,
            )
    else:
        if bar_low <= entry_price - tp_distance:
            return ExitCheckResult(
                should_close=True,
                close_reason="take_profit",
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
    lock_step_r: float = 0.5,
    lock_ratio: float = 0.5,
) -> tuple[Optional[float], bool]:
    """计算 breakeven + 梯度锁利 SL 位置。

    当浮盈达到 breakeven_r 后，SL 移到保本位。
    之后每增加 lock_step_r，锁定更多利润（按 lock_ratio 比例）。

    例：breakeven_r=1.0, lock_step_r=0.5, lock_ratio=0.5
      浮盈 1.0R → SL = entry + 0.1R（保本）
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

    # 梯度锁利：超过 breakeven_r 的部分，按 lock_step_r 步进，锁定 lock_ratio 比例
    if lock_step_r > 0 and lock_ratio > 0 and peak_profit_r > breakeven_r:
        # 锁定金额 = 历史最高浮盈 × lock_ratio
        lock_amount = peak_profit_r * lock_ratio * initial_risk
        # 确保锁定金额至少 ≥ 保本 buffer
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
    config: Optional[ChandelierConfig] = None,
) -> ExitCheckResult:
    """统一出场评估入口。按顺序执行 4 条规则。

    根据 (strategy_category, current_regime) 动态选择出场 profile，
    实现"顺势仓位宽 trail，逆势仓位紧 trail"。

    调用方职责：
      - 每 bar 先更新 peak_price（调用前）
      - 传入当前 regime（每 bar 重新检测的）
      - 记录策略方向到 recent_signal_dirs
      - 将返回的 new_stop_loss / breakeven_activated 更新到持仓对象
    """
    if config is None:
        config = ChandelierConfig()

    # 根据 (category, regime) 查找出场 profile
    if config.regime_aware and strategy_category and current_regime:
        profile = resolve_exit_profile(strategy_category, current_regime)
    else:
        profile = config.fallback_profile

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

    # ③ Chandelier trailing + Breakeven（参数来自 regime-aware profile）
    chandelier_sl = compute_chandelier_sl(
        action, peak_price, current_atr, profile.chandelier_atr_multiplier,
    )

    breakeven_sl, be_activated = compute_breakeven_sl(
        action, entry_price, peak_price, initial_risk,
        profile.breakeven_r, config.breakeven_buffer_r,
        config.min_breakeven_buffer, breakeven_already_activated,
        lock_step_r=config.lock_step_r,
        lock_ratio=profile.lock_ratio,
    )

    new_sl = merge_trailing_sl(action, current_stop_loss, chandelier_sl, breakeven_sl)

    # 合并后 SL 是否被当前 bar 触发？
    check_sl = new_sl if new_sl is not None else current_stop_loss
    if action == "buy" and bar_low <= check_sl:
        return ExitCheckResult(
            should_close=True,
            close_reason="trailing_stop",
            new_stop_loss=new_sl,
            r_multiple=round(r_multiple, 4),
            breakeven_activated=be_activated,
        )
    if action == "sell" and bar_high >= check_sl:
        return ExitCheckResult(
            should_close=True,
            close_reason="trailing_stop",
            new_stop_loss=new_sl,
            r_multiple=round(r_multiple, 4),
            breakeven_activated=be_activated,
        )

    # ④ 信号反转
    if config.signal_exit_enabled and recent_signal_dirs:
        if check_signal_reversal(action, recent_signal_dirs, config.signal_exit_confirmation_bars):
            return ExitCheckResult(
                should_close=True,
                close_reason="signal_exit",
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
                close_reason="timeout",
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
