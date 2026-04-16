"""组合跟踪器：模拟持仓管理。

出场系统：统一出场规则（src/trading/positions/exit_rules.py）
  4 条独立规则：初始 SL / Chandelier trailing / 信号反转 / 超时
  回测和实盘共用同一套纯函数（evaluate_exit）。
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional, Tuple

from src.clients.mt5_market import OHLC
from src.trading.execution.equity_filter import (
    EquityCurveFilter,
    EquityCurveFilterConfig,
)
from src.trading.execution.sizing import TradeParameters
from src.trading.positions.exit_rules import (
    ChandelierConfig,
    WeekendFlatPolicy,
    check_end_of_day,
    check_weekend_flat,
    evaluate_exit,
)

from ..models import TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class _Position:
    """内部持仓表示。"""

    signal_id: str
    strategy: str
    direction: str  # "buy" | "sell"
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    regime: str
    confidence: float
    entry_bar_index: int  # 开仓时的 bar 索引（用于计算 bars_held）
    entry_scope: str = "confirmed"  # "confirmed" | "intrabar"
    atr_at_entry: float = 0.0
    # Chandelier Exit 状态
    initial_risk: float = 0.0  # R = |entry - initial_sl|，开仓时计算，不变
    peak_price: Optional[float] = None  # 持仓期最高价（多头）/最低价（空头）
    bars_held: int = 0
    bars_since_peak: int = 0
    breakeven_activated: bool = False
    recent_signal_dirs: list = field(default_factory=list)  # 最近 N bar 策略方向
    strategy_category: str = ""  # trend / reversion / breakout（legacy）
    exit_spec: dict = field(default_factory=dict)  # 策略 _exit_spec() 输出
    timeframe: str = ""  # 用于 per-TF trail 缩放
    sl_atr_mult: float = 0.0  # 入场 SL 的 ATR 倍数（用于 R 单位保护）
    # 旧体系兼容字段
    breakeven_applied: bool = False
    trailing_active: bool = False
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    # 动态成本累计（非 PnL 单位的原始点数 / USD）
    entry_spread_points: float = 0.0  # 开仓扣的半 spread 点数（USD/lot 侧）
    swap_cost: float = 0.0  # 持仓期间累计 swap（USD，负为亏损）


@dataclass(frozen=True)
class CostModel:
    """动态成本模型（spread + swap）。

    Spread：按时段 × 波动倍数放大基础点差；开平仓各扣一半（买 ask / 卖 bid）。
    Swap：过夜日 UTC 指定小时对每持仓扣一次（周三若启用三倍交割则 × 3）。
    """

    dynamic_spread_enabled: bool = False
    spread_base_points: float = 15.0
    spread_london_mult: float = 1.2
    spread_ny_mult: float = 1.3
    spread_asia_mult: float = 1.0
    spread_volatility_threshold: float = 1.8
    spread_volatility_mult: float = 2.0
    swap_enabled: bool = False
    swap_long_per_lot: float = -0.3
    swap_short_per_lot: float = 0.15
    swap_wednesday_triple: bool = True
    swap_charge_hour_utc: int = 21

    def session_mult(self, bar_time: datetime) -> float:
        """按 UTC 小时粗略分时段：
        亚盘 00-07 / 欧盘 07-12 / 欧美重叠 12-16（取 NY 倍数）/ 美盘 16-21 / 尾盘 21-24。
        """
        h = bar_time.hour
        if 7 <= h < 12:
            return self.spread_london_mult
        if 12 <= h < 21:
            return self.spread_ny_mult
        return self.spread_asia_mult

    def effective_spread_points(self, bar_time: datetime, atr_ratio: float) -> float:
        """返回有效 spread 点数（总值；开平仓各扣一半）。"""
        if not self.dynamic_spread_enabled:
            return 0.0
        spread = self.spread_base_points * self.session_mult(bar_time)
        if atr_ratio >= self.spread_volatility_threshold > 0:
            spread *= self.spread_volatility_mult
        return max(0.0, spread)

    def swap_charge(
        self,
        direction: str,
        position_size: float,
        at_time: datetime,
    ) -> float:
        """返回本次过夜应扣 USD（负数代表亏损）。周三可能 × 3。"""
        if not self.swap_enabled:
            return 0.0
        per_lot = (
            self.swap_long_per_lot if direction == "buy" else self.swap_short_per_lot
        )
        mult = 3.0 if self.swap_wednesday_triple and at_time.weekday() == 2 else 1.0
        return per_lot * position_size * mult


def _resolve_hard_boundary_exit(
    bar: OHLC, pos: "_Position"
) -> Optional[Tuple[float, str]]:
    """同 bar TP/SL 硬边界消歧义。

    返回 (exit_price, reason) 或 None（本 bar 未触硬边界）。

    规则优先级：
    1. bar.open 跳空越过 SL → 按 open 成交（"stop_loss"），broker 实际行为
    2. bar.open 跳空越过 TP → 按 open 成交（"take_profit"）
    3. 同 bar 内 TP 与 SL 都被触及 → SL 获胜（保守回测；OHLC 无法分辨 tick 先后）
    4. 仅触 TP → TP 价成交
    5. 仅触 SL → 返回 None，交给 evaluate_exit 处理（可能是 trailing 后的新 SL）

    该规则避免了旧实现"TP 优先检查导致同 bar 双触时判 TP"造成的胜率虚高。
    """
    sl = pos.stop_loss
    tp = pos.take_profit

    if pos.direction == "buy":
        # 跳空低开越过 SL
        if sl > 0 and bar.open <= sl:
            return bar.open, "stop_loss"
        # 跳空高开越过 TP
        if tp > 0 and bar.open >= tp:
            return bar.open, "take_profit"
        tp_hit = tp > 0 and bar.high >= tp
        sl_hit = sl > 0 and bar.low <= sl
    else:  # sell
        if sl > 0 and bar.open >= sl:
            return bar.open, "stop_loss"
        if tp > 0 and bar.open <= tp:
            return bar.open, "take_profit"
        tp_hit = tp > 0 and bar.low <= tp
        sl_hit = sl > 0 and bar.high >= sl

    if tp_hit and sl_hit:
        # 双触：保守判 SL（无 tick 数据无法判定先后）
        return sl, "stop_loss"
    if tp_hit:
        return tp, "take_profit"
    # 仅 SL 命中或都未命中 → 交给 evaluate_exit（可能带 trailing）
    return None


class PortfolioTracker:
    """模拟组合管理器。

    功能：
    - 模拟开仓/平仓
    - 每根 bar 检查出场（Chandelier Exit 4 规则）
    - 日终自动平仓
    - 追踪资金曲线
    - 记录所有已关闭交易
    """

    def __init__(
        self,
        initial_balance: float,
        max_positions: int = 3,
        commission_per_lot: float = 0.0,
        slippage_points: float = 0.0,
        contract_size: float = 100.0,
        min_volume: float = 0.01,
        max_volume: float = 1.0,
        max_volume_per_order: Optional[float] = None,
        max_volume_per_symbol: Optional[float] = None,
        max_volume_per_day: Optional[float] = None,
        daily_loss_limit_pct: Optional[float] = None,
        max_trades_per_day: Optional[int] = None,
        max_trades_per_hour: Optional[int] = None,
        # Chandelier Exit 出场配置
        chandelier_config: Optional[ChandelierConfig] = None,
        # 日终平仓（与实盘 PositionManager 相同参数）
        end_of_day_close_enabled: bool = False,
        end_of_day_close_hour_utc: int = 21,
        end_of_day_close_minute_utc: int = 0,
        weekend_flat_policy: Optional[WeekendFlatPolicy] = None,
        # 动态成本模型（默认禁用，保持向后兼容）
        cost_model: Optional[CostModel] = None,
        # Equity Curve 过滤器配置：权益跌破 MA 线时暂停开仓，与实盘同一契约
        equity_curve_filter_config: Optional[EquityCurveFilterConfig] = None,
    ) -> None:
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.peak_balance = initial_balance
        self._max_positions = max_positions
        self._commission_per_lot = commission_per_lot
        self._slippage_points = slippage_points
        self._contract_size = contract_size
        self._min_volume = min_volume
        self._max_volume = max_volume
        self._max_volume_per_order = max_volume_per_order
        self._max_volume_per_symbol = max_volume_per_symbol
        self._max_volume_per_day = max_volume_per_day
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._max_trades_per_day = max_trades_per_day
        self._max_trades_per_hour = max_trades_per_hour
        # Chandelier Exit
        self._chandelier_config = chandelier_config or ChandelierConfig()
        # 日终平仓配置
        self._end_of_day_close_enabled = end_of_day_close_enabled
        self._end_of_day_close_hour_utc = end_of_day_close_hour_utc
        self._end_of_day_close_minute_utc = end_of_day_close_minute_utc
        self._last_end_of_day_close_date: Optional[str] = None
        self._weekend_flat_policy = weekend_flat_policy or WeekendFlatPolicy(
            enabled=False
        )
        self._last_weekend_flat_date: Optional[str] = None
        # 成本模型（动态 spread + swap）
        self._cost_model = cost_model or CostModel()

        # Equity Curve Filter（和实盘同一个类）：权益跌破 MA 线时阻止开仓
        # balance_getter 闭包到 self，每次采样读当前 equity
        self._equity_curve_filter = EquityCurveFilter(
            balance_getter=lambda: self.current_balance
            + self._floating_pnl(self._last_bar_close_for_equity),
            config=equity_curve_filter_config or EquityCurveFilterConfig(),
        )
        # 最新 bar close 缓存（record_equity 调用时用，避免 can_open_position 查不到价格）
        self._last_bar_close_for_equity: float = initial_balance
        # 已扣 swap 日期集合：避免同一过夜日重复扣
        self._swap_charged_dates: set[str] = set()
        # 长期 ATR 参考窗口（用于动态 spread 波动倍数）
        self._atr_ref_window: Deque[float] = deque(maxlen=90)

        self._open_positions: List[_Position] = []
        self._closed_trades: List[TradeRecord] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._current_bar_index: int = 0
        # O(1) 日内交易计数（key = date ISO string）
        self._daily_trade_counts: Dict[str, int] = {}
        # 滚动 1 小时交易时间戳窗口（deque 左侧自动淘汰，摊销 O(1)）
        self._hourly_window: Deque[datetime] = deque()
        self._daily_opened_volume: Dict[str, float] = {}
        self._daily_start_equity: Dict[str, float] = {}

    @property
    def closed_trades(self) -> List[TradeRecord]:
        return list(self._closed_trades)

    @property
    def equity_curve(self) -> List[Tuple[datetime, float]]:
        return list(self._equity_curve)

    @property
    def equity_values(self) -> List[float]:
        """纯数值资金曲线（用于统计计算）。"""
        return [v for _, v in self._equity_curve]

    @property
    def open_position_count(self) -> int:
        return len(self._open_positions)

    def record_equity(self, bar: OHLC) -> None:
        """记录当前资金快照（含浮动盈亏）。"""
        floating_pnl = self._floating_pnl(bar.close)
        equity = self.current_balance + floating_pnl
        self._last_bar_close_for_equity = bar.close  # 供 equity_curve_filter 读取
        self._ensure_day_start_equity(bar.time, equity)
        self._equity_curve.append((bar.time, equity))
        # 同步采样到 equity_curve_filter（与实盘 pre_trade_checks 的 record_equity() 对齐）
        self._equity_curve_filter.record_equity()

    def observe_bar(self, bar: OHLC) -> None:
        """记录日初权益基线，供日损限制使用。"""
        self._ensure_day_start_equity(
            bar.time,
            self.current_balance + self._floating_pnl(bar.open),
        )

    def update_atr_reference(self, atr: float) -> None:
        """每根 bar 传入当前 ATR，供动态 spread 波动倍数参考。"""
        if atr > 0:
            self._atr_ref_window.append(atr)

    def _current_atr_ratio(self) -> float:
        """当前 ATR / 90-bar 均值 ATR（未就绪返回 1.0）。"""
        if len(self._atr_ref_window) < 10:
            return 1.0
        current = self._atr_ref_window[-1]
        avg = sum(self._atr_ref_window) / len(self._atr_ref_window)
        return current / avg if avg > 0 else 1.0

    def apply_overnight_swap(self, bar: OHLC) -> None:
        """若 bar 跨越过夜时刻，对所有持仓按方向扣除 swap。

        过夜时刻定义：bar.time 的 UTC 小时 == swap_charge_hour_utc，且该日未扣过。
        """
        cm = self._cost_model
        if not cm.swap_enabled or not self._open_positions:
            return
        if bar.time.hour != cm.swap_charge_hour_utc:
            return
        day_key = bar.time.date().isoformat()
        if day_key in self._swap_charged_dates:
            return
        self._swap_charged_dates.add(day_key)
        for pos in self._open_positions:
            charge = cm.swap_charge(pos.direction, pos.position_size, bar.time)
            if charge == 0.0:
                continue
            pos.swap_cost += charge
            self.current_balance += charge

    def can_open_position(
        self,
        bar: OHLC,
        trade_params: TradeParameters,
    ) -> tuple[bool, str]:
        """检查开仓是否满足回测风控限制。"""
        if len(self._open_positions) >= self._max_positions:
            return False, "max_positions"

        volume = float(trade_params.position_size)
        if volume < self._min_volume:
            return False, "min_volume"
        if volume > self._max_volume:
            return False, "max_volume"
        if (
            self._max_volume_per_order is not None
            and volume > self._max_volume_per_order
        ):
            return False, "max_volume_per_order"

        open_volume = sum(pos.position_size for pos in self._open_positions)
        if (
            self._max_volume_per_symbol is not None
            and open_volume + volume > self._max_volume_per_symbol
        ):
            return False, "max_volume_per_symbol"

        day_key = bar.time.date().isoformat()
        day_opened_volume = self._daily_opened_volume.get(day_key, 0.0)
        if (
            self._max_volume_per_day is not None
            and day_opened_volume + volume > self._max_volume_per_day
        ):
            return False, "max_volume_per_day"

        if self._max_trades_per_day is not None and self._max_trades_per_day > 0:
            trades_today = self._daily_trade_counts.get(day_key, 0)
            if trades_today >= self._max_trades_per_day:
                return False, "max_trades_per_day"

        if self._max_trades_per_hour is not None and self._max_trades_per_hour > 0:
            hour_ago = bar.time - timedelta(hours=1)
            # 修剪滑出 1 小时窗口的旧记录（摊销 O(1)）
            while self._hourly_window and self._hourly_window[0] < hour_ago:
                self._hourly_window.popleft()
            if len(self._hourly_window) >= self._max_trades_per_hour:
                return False, "max_trades_per_hour"

        # _floating_pnl 只计算一次，避免重复遍历所有持仓
        current_close_equity = self.current_balance + self._floating_pnl(bar.close)
        self._ensure_day_start_equity(bar.time, current_close_equity)
        start_equity = self._daily_start_equity.get(day_key)
        if (
            self._daily_loss_limit_pct is not None
            and start_equity is not None
            and start_equity > 0
        ):
            loss_pct = max(
                0.0, ((start_equity - current_close_equity) / start_equity) * 100.0
            )
            if loss_pct >= self._daily_loss_limit_pct:
                return False, "daily_loss_limit_pct"

        # Equity curve filter：与实盘 pre_trade_checks.py 的 should_block() 同契约
        if self._equity_curve_filter.should_block():
            return False, "equity_curve_below_ma"

        return True, ""

    def open_position(
        self,
        strategy: str,
        action: str,
        bar: OHLC,
        trade_params: TradeParameters,
        regime: str,
        confidence: float,
        bar_index: int,
        atr_at_entry: float = 0.0,
        strategy_category: str = "",
        timeframe: str = "",
        exit_spec: Optional[dict] = None,
        fill_price: Optional[float] = None,
        entry_scope: str = "confirmed",
    ) -> bool:
        """尝试开仓。

        fill_price: 指定成交基准价。Pending 单用触发边界价（含跳空场景的 bar.open），
                    None 时退化为 bar.close（market 入场）。滑点在此基础上再叠加。

        Returns:
            是否成功开仓。
        """
        allowed, _ = self.can_open_position(bar, trade_params)
        if not allowed:
            return False

        base_price = float(fill_price) if fill_price is not None else bar.close
        # 动态 spread 的一半（开仓承担），叠加固定 slippage_points（非 spread 类滑点）
        atr_ratio = self._current_atr_ratio()
        entry_half_spread = (
            self._cost_model.effective_spread_points(bar.time, atr_ratio) / 2.0
        )
        unfavorable = self._slippage_points + entry_half_spread
        if action == "buy":
            entry_price = base_price + unfavorable
        else:
            entry_price = base_price - unfavorable

        # 扣除手续费
        commission = self._commission_per_lot * trade_params.position_size
        self.current_balance -= commission

        signal_id = f"bt_{uuid.uuid4().hex[:8]}"
        initial_risk = abs(entry_price - trade_params.stop_loss)
        pos = _Position(
            signal_id=signal_id,
            strategy=strategy,
            direction=action,
            entry_time=bar.time,
            entry_price=entry_price,
            stop_loss=trade_params.stop_loss,
            take_profit=trade_params.take_profit,
            position_size=trade_params.position_size,
            regime=regime,
            confidence=confidence,
            entry_bar_index=bar_index,
            atr_at_entry=atr_at_entry,
            initial_risk=initial_risk,
            peak_price=entry_price,
            strategy_category=strategy_category,
            exit_spec=dict(exit_spec) if exit_spec else {},
            timeframe=timeframe,
            sl_atr_mult=(
                round(trade_params.sl_distance / atr_at_entry, 4)
                if atr_at_entry > 0
                else 0.0
            ),
            highest_price=entry_price if action == "buy" else None,
            lowest_price=entry_price if action == "sell" else None,
            entry_spread_points=entry_half_spread,
            entry_scope=entry_scope,
        )
        self._open_positions.append(pos)
        day_key = bar.time.date().isoformat()
        self._daily_trade_counts[day_key] = self._daily_trade_counts.get(day_key, 0) + 1
        self._hourly_window.append(bar.time)
        self._daily_opened_volume[day_key] = (
            self._daily_opened_volume.get(day_key, 0.0) + trade_params.position_size
        )
        # 交易事件自动记录资金快照（精确捕捉开仓时刻的资金变化）
        self._equity_curve.append(
            (bar.time, self.current_balance + self._floating_pnl(bar.close))
        )
        return True

    def _ensure_day_start_equity(self, timestamp: datetime, equity: float) -> None:
        day_key = timestamp.date().isoformat()
        self._daily_start_equity.setdefault(day_key, equity)

    def check_exits(
        self,
        bar: OHLC,
        bar_index: int,
        indicators: Dict[str, Dict] = {},  # noqa: B006
        *,
        current_atr: float = 0.0,
        current_regime: str = "",
        signal_directions: Optional[Dict[str, str]] = None,
    ) -> List[TradeRecord]:
        """检查所有持仓是否应平仓。

        使用 Chandelier Exit 4 规则系统：
        1. 初始 SL 触发
        2. Chandelier trailing（current_ATR 动态）+ Breakeven 保护
        3. 信号反转 N-bar 确认
        4. 超时退出

        Args:
            bar: 当前 OHLC bar
            bar_index: bar 索引
            indicators: 指标数据（未使用，保留接口兼容）
            current_atr: 当前 ATR 值（Chandelier trailing 用）
            signal_directions: 当前 bar 各策略/投票组方向
                {"momentum_vote": "buy", "trend_triple_confirm": "sell", ...}
        """
        # 先检查周末强平（防周一跳空）—— 优先级高于每日 EOD
        if (
            check_weekend_flat(
                current_time=bar.time,
                policy=self._weekend_flat_policy,
                last_flat_date=self._last_weekend_flat_date,
            )
            and self._open_positions
        ):
            self._last_weekend_flat_date = bar.time.date().isoformat()
            return self._close_all_with_reason(bar, bar_index, "weekend_flat")

        # 日终平仓
        if self._end_of_day_close_enabled:
            eod_triggered = check_end_of_day(
                current_time=bar.time,
                close_hour_utc=self._end_of_day_close_hour_utc,
                close_minute_utc=self._end_of_day_close_minute_utc,
                last_close_date=self._last_end_of_day_close_date,
            )
            if eod_triggered and self._open_positions:
                self._last_end_of_day_close_date = bar.time.date().isoformat()
                return self._close_all_with_reason(bar, bar_index, "end_of_day")

        closed: List[TradeRecord] = []
        remaining: List[_Position] = []
        sig_dirs = signal_directions or {}

        for pos in self._open_positions:
            # 记录 bar 开盘前 peak（供 evaluate_exit 做 look-ahead 防护的 SL 判定用）
            peak_at_bar_open = pos.peak_price or pos.entry_price
            # 更新 peak price（含本 bar 的 high/low，供下一 bar 使用）
            if pos.direction == "buy":
                new_peak = max(peak_at_bar_open, bar.high)
                if new_peak > peak_at_bar_open:
                    pos.peak_price = new_peak
                    pos.bars_since_peak = 0
                else:
                    pos.bars_since_peak += 1
                pos.highest_price = pos.peak_price
            else:
                new_peak = min(peak_at_bar_open, bar.low)
                if new_peak < peak_at_bar_open:
                    pos.peak_price = new_peak
                    pos.bars_since_peak = 0
                else:
                    pos.bars_since_peak += 1
                pos.lowest_price = pos.peak_price

            pos.bars_held += 1

            # 硬边界 TP/SL 同 bar 触发消歧义（替代旧的 TP-优先判定）
            # 规则：
            # 1) bar.open 跳空越过 TP/SL → 按 bar.open 成交（realistic broker fill）
            # 2) 同 bar 内 TP 和 SL 都被触及（无 tick 数据无法分辨先后）→ SL 获胜
            #    行业惯例：无 tick 时按最坏情况判定，避免虚高胜率
            # 3) 只触 TP → TP 成交；只触 SL → 交给 evaluate_exit（处理 trailing）
            hard_exit = _resolve_hard_boundary_exit(bar, pos)
            if hard_exit is not None:
                exit_price, hard_reason = hard_exit
                trade = self._close_position(
                    pos,
                    exit_price,
                    bar.time,
                    bar_index,
                    hard_reason,
                )
                closed.append(trade)
                continue

            # 记录策略方向到 recent_signal_dirs
            strategy_dir = sig_dirs.get(pos.strategy, "")
            if strategy_dir:
                pos.recent_signal_dirs.append(strategy_dir)
                # 只保留最近 N 条（N = confirmation_bars + 缓冲）
                max_keep = self._chandelier_config.signal_exit_confirmation_bars + 2
                if len(pos.recent_signal_dirs) > max_keep:
                    pos.recent_signal_dirs = pos.recent_signal_dirs[-max_keep:]

            # Chandelier Exit 统一评估（regime-aware + per-TF 缩放 + R 单位保护）
            # peak_price_at_bar_open 防止本 bar 刚冲出新高触发的 trailing SL 反手在同 bar 被打到（look-ahead）
            result = evaluate_exit(
                action=pos.direction,
                entry_price=pos.entry_price,
                bar_high=bar.high,
                bar_low=bar.low,
                bar_close=bar.close,
                current_stop_loss=pos.stop_loss,
                initial_risk=pos.initial_risk,
                peak_price=pos.peak_price or pos.entry_price,
                current_atr=current_atr,
                bars_held=pos.bars_held,
                breakeven_already_activated=pos.breakeven_activated,
                recent_signal_dirs=pos.recent_signal_dirs,
                strategy_category=pos.strategy_category,
                current_regime=current_regime,
                timeframe=pos.timeframe,
                initial_sl_atr_mult=pos.sl_atr_mult,
                config=self._chandelier_config,
                exit_spec=getattr(pos, "exit_spec", None),
                peak_price_at_bar_open=peak_at_bar_open,
            )

            # 更新持仓状态
            pos.breakeven_activated = result.breakeven_activated
            if result.new_stop_loss is not None:
                pos.stop_loss = result.new_stop_loss

            if result.should_close:
                # 确定平仓价格
                if result.close_reason == "take_profit":
                    # TP 按硬上界价格成交
                    tp_dist = self._chandelier_config.max_tp_r * pos.initial_risk
                    if pos.direction == "buy":
                        exit_price = pos.entry_price + tp_dist
                    else:
                        exit_price = pos.entry_price - tp_dist
                elif result.close_reason in ("stop_loss", "trailing_stop"):
                    exit_price = pos.stop_loss
                else:
                    # signal_exit / timeout → 按 bar.close 平仓
                    exit_price = bar.close

                trade = self._close_position(
                    pos,
                    exit_price,
                    bar.time,
                    bar_index,
                    result.close_reason,
                )
                closed.append(trade)
            else:
                remaining.append(pos)

        self._open_positions = remaining
        return closed

    def close_by_signal(
        self,
        strategy: str,
        bar: OHLC,
        bar_index: int,
    ) -> List[TradeRecord]:
        """按策略名关闭对应的持仓（信号退出）。"""
        closed: List[TradeRecord] = []
        remaining: List[_Position] = []

        for pos in self._open_positions:
            if pos.strategy == strategy:
                trade = self._close_position(
                    pos, bar.close, bar.time, bar_index, "signal_exit"
                )
                closed.append(trade)
            else:
                remaining.append(pos)

        self._open_positions = remaining
        return closed

    def close_all(self, bar: OHLC, bar_index: int) -> List[TradeRecord]:
        """强制平仓所有持仓。"""
        return self._close_all_with_reason(bar, bar_index, "end_of_test")

    def _close_all_with_reason(
        self, bar: OHLC, bar_index: int, reason: str
    ) -> List[TradeRecord]:
        """按指定原因平仓所有持仓。"""
        closed: List[TradeRecord] = []
        for pos in self._open_positions:
            trade = self._close_position(pos, bar.close, bar.time, bar_index, reason)
            closed.append(trade)
        self._open_positions = []
        return closed

    def _close_position(
        self,
        pos: _Position,
        exit_price: float,
        exit_time: datetime,
        bar_index: int,
        exit_reason: str,
    ) -> TradeRecord:
        """关闭持仓并计算 PnL。"""
        # 平仓承担另一半 spread + 固定 slippage_points（方向与开仓相反）
        atr_ratio = self._current_atr_ratio()
        exit_half_spread = (
            self._cost_model.effective_spread_points(exit_time, atr_ratio) / 2.0
        )
        unfavorable = self._slippage_points + exit_half_spread
        if pos.direction == "buy":
            actual_exit = exit_price - unfavorable
        else:
            actual_exit = exit_price + unfavorable

        if pos.direction == "buy":
            price_diff = actual_exit - pos.entry_price
        else:
            price_diff = pos.entry_price - actual_exit

        pnl = price_diff * pos.position_size * self._contract_size
        # 扣除出场手续费
        exit_commission = self._commission_per_lot * pos.position_size
        entry_commission = self._commission_per_lot * pos.position_size
        total_commission = entry_commission + exit_commission
        pnl -= exit_commission

        # swap 已在持仓期按日累加到 current_balance（apply_overnight_swap）；
        # 这里只在 TradeRecord 中记录累计值供审计。
        swap_cost_total = pos.swap_cost

        # 总点数级成本 = 固定滑点 ×2 + 开/平 spread
        total_cost_points = (
            self._slippage_points * 2 + pos.entry_spread_points + exit_half_spread
        )
        slippage_cost = total_cost_points * pos.position_size * self._contract_size

        # 使用初始资金作为分母，避免资金耗尽时结果失真
        pnl_pct = (
            (pnl / self.initial_balance * 100.0) if self.initial_balance > 0 else 0.0
        )

        self.current_balance += pnl
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
        # 交易事件自动记录资金快照（精确捕捉平仓时刻的资金变化）
        self._equity_curve.append((exit_time, self.current_balance))

        bars_held = bar_index - pos.entry_bar_index

        trade = TradeRecord(
            signal_id=pos.signal_id,
            strategy=pos.strategy,
            direction=pos.direction,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=exit_time,
            exit_price=actual_exit,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            position_size=pos.position_size,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 4),
            bars_held=bars_held,
            regime=pos.regime,
            confidence=pos.confidence,
            exit_reason=exit_reason,
            entry_scope=pos.entry_scope,
            slippage_cost=round(slippage_cost, 2),
            commission_cost=round(total_commission, 2),
            swap_cost=round(swap_cost_total, 2),
        )
        self._closed_trades.append(trade)
        return trade

    def _floating_pnl(self, current_price: float) -> float:
        """计算所有持仓的浮动盈亏。"""
        total = 0.0
        for pos in self._open_positions:
            if pos.direction == "buy":
                diff = current_price - pos.entry_price
            else:
                diff = pos.entry_price - current_price
            total += diff * pos.position_size * self._contract_size
        return total
