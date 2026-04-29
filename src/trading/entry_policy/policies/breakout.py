"""BreakoutEntryPolicy — 突破确认入场（B 方案）。

设计：等市场创新高/新低（突破最近 N 根 bar 极端）才入场。STOP 单挂在
recent_high + buffer / recent_low - buffer 处。

适用：breakout_follow / open_range_breakout / pullback_window 等突破型策略。
解决 "形态信号 + close MARKET" 的 mean reversion 风险——不创新高/低就不入场。

参数（policy_params.breakout）：
  - lookback_bars: 用最近 N 根 bar 找极端（默认 1，仅信号 bar 自身）
  - buffer_atr: 极端价 ± buffer_atr × ATR 作 STOP 触发价（默认 0.05）
  - zone_atr: 监控区间宽度（默认 0.05）
  - validity_bars_<TF>: per-TF 限价存活 bar 数
"""

from __future__ import annotations

from typing import Any

from src.trading.entry_policy.intent import EntryIntent, MarketSnapshot
from src.trading.entry_policy.port import PolicyParams
from src.trading.entry_policy.specs import (
    EntrySpecGroup,
    EntrySpecMember,
    EntryType,
    new_group_id,
)


def _params_get_float(params: PolicyParams, key: str, default: float) -> float:
    value = params.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _params_get_int(params: PolicyParams, key: str, default: int) -> int:
    value = params.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _params_get_validity(params: PolicyParams, timeframe: str) -> int | None:
    key = f"validity_bars_{timeframe}"
    if key not in params:
        return None
    try:
        return int(params[key])
    except (TypeError, ValueError):
        return None


class BreakoutEntryPolicy:
    """突破确认入场（B 方案）。"""

    name = "breakout"

    def derive(
        self,
        intent: EntryIntent,
        market: MarketSnapshot,
        params: PolicyParams,
    ) -> EntrySpecGroup:
        lookback = max(1, _params_get_int(params, "lookback_bars", 1))
        buffer_atr = _params_get_float(params, "buffer_atr", 0.05)
        zone_atr = _params_get_float(params, "zone_atr", 0.05)
        validity = _params_get_validity(params, intent.timeframe)

        recent = market.recent_bars[-lookback:]
        if not recent:
            recent = (market.signal_bar,)

        if intent.direction == "buy":
            extreme = max(b.high for b in recent)
            trigger_price = extreme + buffer_atr * market.atr_value
        else:
            extreme = min(b.low for b in recent)
            trigger_price = extreme - buffer_atr * market.atr_value

        zone_half = zone_atr * market.atr_value
        entry_low = trigger_price - zone_half
        entry_high = trigger_price + zone_half

        member = EntrySpecMember(
            member_id="stop_breakout",
            entry_type=EntryType.STOP,
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
                "branch": f"breakout_lookback_{lookback}",
                "pattern_type": intent.pattern_type.value,
                "extreme": round(extreme, 6),
                "buffer_atr": buffer_atr,
                "zone_atr": zone_atr,
                "lookback_bars": lookback,
            },
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "single_member_stop_breakout",
            "params_schema": {
                "lookback_bars": "int (default 1)",
                "buffer_atr": "float (default 0.05)",
                "zone_atr": "float (default 0.05)",
                "validity_bars_<TF>": "int (per-TF, default None=use global)",
            },
            "description": (
                "Confirmation breakout entry: STOP order at recent extreme ± "
                "buffer_atr×ATR, fires only when price actually breaks past."
            ),
        }
