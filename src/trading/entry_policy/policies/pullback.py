"""PullbackEntryPolicy — 形态特化的限价回踩入场（A 方案）。

核心 IP：根据 PatternType 选不同回踩锚点 + offset，trigger_price 算好后挂 LIMIT 单。
解决 "close MARKET 入场 → 必在 bar 极端 → next bar 反抽" 的盈亏比塌方问题。

形态分支表（match intent.pattern_type）：
| Pattern              | anchor                                         | offset×ATR |
|----------------------|------------------------------------------------|------------|
| PIN_BULL / HAMMER    | bar.low + (body_low - bar.low) × wick_pct      | 0.05       |
| PIN_BEAR / SHOOTING_STAR | bar.high - (bar.high - body_high) × wick_pct | 0.05    |
| ENGULFING_BULL       | prev_bar.high                                  | 0.10       |
| ENGULFING_BEAR       | prev_bar.low                                   | 0.10       |
| BIG_BAR_BULL/BEAR    | bar.body_low + 0.5 × (body_high - body_low)    | 0.10       |
| THREE_SOLDIERS/CROWS | bars[-3].close（首根 bar 收盘）                | 0.15       |
| REJECTION_BULL       | bar.low + 0.2 × ATR                            | 0.20       |
| REJECTION_BEAR       | bar.high - 0.2 × ATR                           | 0.20       |
| SWEEP_REVERSAL       | bar.mid                                        | 0.15       |
| TRENDLINE_TOUCH_*    | intent.signal_metadata["trendline_price"]      | 0.10       |
| fallback             | current_close                                  | 0.30       |

LIMIT 触发语义：buy → trigger_price + zone_atr 上方监控（向下回踩到此价成交）；
sell → trigger_price - zone_atr 下方监控（向上回踩到此价成交）。

参数（policy_params.pullback）：
  - zone_atr: 限价 buffer（默认 0.10）
  - wick_pct: 影线回踩百分比（默认 0.50）
  - branch_<pattern>_offset: 各形态的 offset 覆盖（默认见 _DEFAULT_OFFSETS）
  - validity_bars_<TF>: per-TF 限价存活 bar 数
"""

from __future__ import annotations

from typing import Any

from src.trading.entry_policy.intent import BarSnapshot, EntryIntent, MarketSnapshot
from src.trading.entry_policy.pattern import PatternType
from src.trading.entry_policy.port import PolicyParams
from src.trading.entry_policy.specs import (
    EntrySpecGroup,
    EntrySpecMember,
    EntryType,
    new_group_id,
)

_DEFAULT_OFFSETS: dict[PatternType, float] = {
    PatternType.PIN_BULL: 0.05,
    PatternType.PIN_BEAR: 0.05,
    PatternType.HAMMER: 0.05,
    PatternType.SHOOTING_STAR: 0.05,
    PatternType.ENGULFING_BULL: 0.10,
    PatternType.ENGULFING_BEAR: 0.10,
    PatternType.BIG_BAR_BULL: 0.10,
    PatternType.BIG_BAR_BEAR: 0.10,
    PatternType.THREE_SOLDIERS: 0.15,
    PatternType.THREE_CROWS: 0.15,
    PatternType.REJECTION_BULL: 0.20,
    PatternType.REJECTION_BEAR: 0.20,
    PatternType.SWEEP_REVERSAL: 0.15,
    PatternType.TRENDLINE_TOUCH_BULL: 0.10,
    PatternType.TRENDLINE_TOUCH_BEAR: 0.10,
}
_FALLBACK_OFFSET = 0.30


def _params_get_float(params: PolicyParams, key: str, default: float) -> float:
    value = params.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _params_get_int(params: PolicyParams, key: str, default: int | None) -> int | None:
    if key not in params:
        return default
    try:
        return int(params[key])
    except (TypeError, ValueError):
        return default


def _resolve_branch_offset(params: PolicyParams, pattern: PatternType) -> float:
    """从 ini 读 branch_<pattern>_offset；缺失走 _DEFAULT_OFFSETS / fallback。"""
    key = f"branch_{pattern.value}_offset"
    if key in params:
        return _params_get_float(
            params, key, _DEFAULT_OFFSETS.get(pattern, _FALLBACK_OFFSET)
        )
    return _DEFAULT_OFFSETS.get(pattern, _FALLBACK_OFFSET)


def _wick_pct(params: PolicyParams) -> float:
    return _params_get_float(params, "wick_pct", 0.50)


def _zone_atr(params: PolicyParams) -> float:
    return _params_get_float(params, "zone_atr", 0.10)


def _validity_bars(params: PolicyParams, timeframe: str) -> int | None:
    return _params_get_int(params, f"validity_bars_{timeframe}", None)


# ── 形态 → anchor 计算函数 ──────────────────────────────────────────────


def _anchor_pin_or_hammer_bull(bar: BarSnapshot, wick_pct: float) -> float:
    return bar.low + (bar.body_low - bar.low) * wick_pct


def _anchor_pin_or_shooting_bear(bar: BarSnapshot, wick_pct: float) -> float:
    return bar.high - (bar.high - bar.body_high) * wick_pct


def _anchor_engulfing(
    prev: BarSnapshot | None, fallback: float, *, is_bull: bool
) -> float:
    if prev is None:
        return fallback
    return prev.high if is_bull else prev.low


def _anchor_body_50pct(bar: BarSnapshot) -> float:
    return (bar.body_low + bar.body_high) / 2.0


def _anchor_three_method(bars: tuple[BarSnapshot, ...], fallback: float) -> float:
    """连续 3 根形态用首根 bar 收盘价作 anchor。bars 不足时 fallback。"""
    if len(bars) < 3:
        return fallback
    return bars[-3].close


def _anchor_rejection(
    bar: BarSnapshot, atr: float, buffer_atr: float, *, is_bull: bool
) -> float:
    if is_bull:
        return bar.low + buffer_atr * atr
    return bar.high - buffer_atr * atr


def _anchor_trendline(intent: EntryIntent, fallback: float) -> float:
    """trendline_touch 由 base.py evaluate 写入 metadata['trendline_price']。"""
    raw = intent.signal_metadata.get("trendline_price")
    if raw is None:
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _compute_anchor(
    intent: EntryIntent,
    market: MarketSnapshot,
    wick_pct: float,
) -> tuple[float, str]:
    """根据 pattern_type 算 anchor 价；返回 (anchor_price, branch_label)。

    branch_label 写入 metadata 用于审计/切片分析。
    """
    pattern = intent.pattern_type
    bar = market.signal_bar
    prev = market.prev_bar
    fallback = market.current_close

    if pattern in (PatternType.PIN_BULL, PatternType.HAMMER):
        return _anchor_pin_or_hammer_bull(bar, wick_pct), pattern.value
    if pattern in (PatternType.PIN_BEAR, PatternType.SHOOTING_STAR):
        return _anchor_pin_or_shooting_bear(bar, wick_pct), pattern.value
    if pattern == PatternType.ENGULFING_BULL:
        return _anchor_engulfing(prev, fallback, is_bull=True), pattern.value
    if pattern == PatternType.ENGULFING_BEAR:
        return _anchor_engulfing(prev, fallback, is_bull=False), pattern.value
    if pattern in (PatternType.BIG_BAR_BULL, PatternType.BIG_BAR_BEAR):
        return _anchor_body_50pct(bar), pattern.value
    if pattern in (PatternType.THREE_SOLDIERS, PatternType.THREE_CROWS):
        return _anchor_three_method(market.recent_bars, fallback), pattern.value
    if pattern == PatternType.REJECTION_BULL:
        return (
            _anchor_rejection(bar, market.atr_value, 0.2, is_bull=True),
            pattern.value,
        )
    if pattern == PatternType.REJECTION_BEAR:
        return (
            _anchor_rejection(bar, market.atr_value, 0.2, is_bull=False),
            pattern.value,
        )
    if pattern == PatternType.SWEEP_REVERSAL:
        return bar.mid, pattern.value
    if pattern in (PatternType.TRENDLINE_TOUCH_BULL, PatternType.TRENDLINE_TOUCH_BEAR):
        return _anchor_trendline(intent, fallback), pattern.value
    # NONE / BREAKOUT_*（不属本 policy 适用范围）→ fallback
    return fallback, "fallback"


class PullbackEntryPolicy:
    """形态特化的限价回踩入场（A 方案）。"""

    name = "pullback"

    def derive(
        self,
        intent: EntryIntent,
        market: MarketSnapshot,
        params: PolicyParams,
    ) -> EntrySpecGroup:
        wick_pct = _wick_pct(params)
        zone_atr = _zone_atr(params)
        offset_atr = _resolve_branch_offset(params, intent.pattern_type)
        validity = _validity_bars(params, intent.timeframe)

        anchor, branch_label = _compute_anchor(intent, market, wick_pct)

        # 把 anchor 朝有利于成交的方向轻微外推：buy 限价略高于 anchor，sell 略低
        # 这样如果价格已经跌穿 anchor，挂单价仍能被反弹回测到。
        if intent.direction == "buy":
            trigger_price = anchor + offset_atr * market.atr_value
            entry_low = trigger_price - zone_atr * market.atr_value
            entry_high = trigger_price + zone_atr * market.atr_value
        else:
            trigger_price = anchor - offset_atr * market.atr_value
            entry_low = trigger_price - zone_atr * market.atr_value
            entry_high = trigger_price + zone_atr * market.atr_value

        member = EntrySpecMember(
            member_id="limit_pullback",
            entry_type=EntryType.LIMIT,
            trigger_price=round(trigger_price, 6),
            entry_low=round(entry_low, 6),
            entry_high=round(entry_high, 6),
            validity_bars=validity,
        )
        return EntrySpecGroup(
            group_id=new_group_id(),
            members=(member,),
            cancellation_policy="any_fill",
            metadata={
                "policy_name": self.name,
                "branch": branch_label,
                "pattern_type": intent.pattern_type.value,
                "anchor": round(anchor, 6),
                "offset_atr": offset_atr,
                "zone_atr": zone_atr,
            },
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "single_member_limit_pullback",
            "params_schema": {
                "zone_atr": "float (default 0.10)",
                "wick_pct": "float (default 0.50)",
                "branch_<pattern>_offset": "float (per-pattern, default see _DEFAULT_OFFSETS)",
                "validity_bars_<TF>": "int (per-TF, default None=use global)",
            },
            "description": (
                "Pattern-aware LIMIT pullback entry: derives anchor by PatternType, "
                "places LIMIT order at anchor ± offset×ATR with zone_atr buffer."
            ),
        }
