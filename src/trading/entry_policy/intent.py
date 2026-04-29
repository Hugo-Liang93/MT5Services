"""EntryIntent / MarketSnapshot / BarSnapshot — EntryPolicy 输入 DTO。

Policy 是纯函数式：`derive(intent, market, params) -> EntrySpecGroup`。
为遵守 ADR-013 边界，输入必须是 frozen 值对象，policy 不能 reach back 到任何
运行时（SignalRuntime / PendingEntryManager / 策略实例）。

BarSnapshot 是从 metadata[MK.RECENT_BARS] 中提取的轻量 OHLC 视图：
- live: 由 signals.service._inject_recent_bars 写入（dataclasses.asdict 形态）
- backtest: 由 backtesting.engine.signals 写入（同样 dict 形态）
两端都给 dict[str, Any]，本模块的 BarSnapshot.from_mapping() 统一转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Optional, Tuple

from src.trading.entry_policy.pattern import PatternType

Direction = Literal["buy", "sell"]


@dataclass(frozen=True)
class BarSnapshot:
    """OHLC bar 的 frozen 视图。Policy 用 bar.high / bar.low 等属性访问。"""

    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    time: Optional[datetime] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BarSnapshot":
        """从 metadata RECENT_BARS 元素构造。容忍 time 字段缺失（向后兼容）。"""
        return cls(
            open=float(raw["open"]),
            high=float(raw["high"]),
            low=float(raw["low"]),
            close=float(raw["close"]),
            volume=float(raw.get("volume", 0.0) or 0.0),
            time=raw.get("time"),
        )

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_range(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - self.body_high

    @property
    def lower_wick(self) -> float:
        return self.body_low - self.low

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass(frozen=True)
class EntryIntent:
    """信号策略输出的入场意图。

    pattern_type 是结构化形态枚举（解除 why_reason 字符串解析的脆弱依赖）。
    signal_metadata 是 SignalDecision.metadata 的只读视图，policy 可读取
    why/when/where/structure_bias/trendline_price 等字段做精细决策。
    """

    strategy_name: str
    timeframe: str
    direction: Direction
    confidence: float
    bar_time: datetime
    pattern_type: PatternType
    signal_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketSnapshot:
    """触发时刻的市场状态 frozen 视图。

    recent_bars 至少含信号 bar（[-1]）+ 前一根（[-2]，用于 engulfing prev_extreme）。
    atr_value 必须 > 0（policy 内部用作 buffer 单位）。
    """

    recent_bars: Tuple[BarSnapshot, ...]
    atr_value: float
    current_close: float
    current_atr_source: str = "confirmed"
    market_structure: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.atr_value <= 0:
            raise ValueError(
                f"MarketSnapshot.atr_value must be > 0, got {self.atr_value!r}"
            )
        if not self.recent_bars:
            raise ValueError("MarketSnapshot.recent_bars must contain at least 1 bar")

    @property
    def signal_bar(self) -> BarSnapshot:
        """信号触发的当前 bar（recent_bars[-1]）。"""
        return self.recent_bars[-1]

    @property
    def prev_bar(self) -> Optional[BarSnapshot]:
        """前一根 bar（recent_bars[-2]）；不足时为 None，policy 自行处理 fallback。"""
        if len(self.recent_bars) < 2:
            return None
        return self.recent_bars[-2]


__all__ = [
    "BarSnapshot",
    "Direction",
    "EntryIntent",
    "MarketSnapshot",
]
