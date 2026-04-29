"""FibPullbackEntryPolicy — 斐波那契回撤入场（深度回调备选）。

适用：trendline_touch / strong_trend_follow 等 "趋势深度回调" 策略。
计算：从 recent_bars 找最近 swing high/low，按 fib_levels 中第一个未被价格突破的
档位作 anchor 挂 LIMIT。

参数（policy_params.fib_pullback）：
  - fib_levels: list of float（默认 [0.382, 0.5, 0.618]）
  - swing_lookback: int（找 swing 的回看 bar 数，默认 20）
  - zone_atr: 限价 buffer（默认 0.10）
  - validity_bars_<TF>: per-TF 限价存活 bar 数

Swing 检测简化：取 swing_lookback 内最大 high 与最小 low 作 swing range。
（生产级 swing 检测可在 P3+ 接入 src/indicators/core/price_structure 输出。）
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

_DEFAULT_FIB_LEVELS: tuple[float, ...] = (0.382, 0.5, 0.618)
_DEFAULT_SWING_LOOKBACK = 20


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


def _params_get_fib_levels(params: PolicyParams) -> tuple[float, ...]:
    raw = params.get("fib_levels")
    if raw is None:
        return _DEFAULT_FIB_LEVELS
    if isinstance(raw, (list, tuple)):
        try:
            cleaned = tuple(float(item) for item in raw)
        except (TypeError, ValueError):
            return _DEFAULT_FIB_LEVELS
        # 过滤 [0, 1] 内合法值
        valid = tuple(level for level in cleaned if 0.0 < level < 1.0)
        return valid or _DEFAULT_FIB_LEVELS
    return _DEFAULT_FIB_LEVELS


def _params_get_validity(params: PolicyParams, timeframe: str) -> int | None:
    key = f"validity_bars_{timeframe}"
    if key not in params:
        return None
    try:
        return int(params[key])
    except (TypeError, ValueError):
        return None


class FibPullbackEntryPolicy:
    """斐波那契回撤入场。"""

    name = "fib_pullback"

    def derive(
        self,
        intent: EntryIntent,
        market: MarketSnapshot,
        params: PolicyParams,
    ) -> EntrySpecGroup:
        fib_levels = _params_get_fib_levels(params)
        lookback = max(
            2, _params_get_int(params, "swing_lookback", _DEFAULT_SWING_LOOKBACK)
        )
        zone_atr = _params_get_float(params, "zone_atr", 0.10)
        validity = _params_get_validity(params, intent.timeframe)

        # 取 lookback 内的 swing range（简化：max high / min low）
        recent = (
            market.recent_bars[-lookback:]
            if len(market.recent_bars) >= 2
            else market.recent_bars
        )
        swing_high = max(b.high for b in recent)
        swing_low = min(b.low for b in recent)
        swing_range = swing_high - swing_low

        if swing_range <= 0 or len(recent) < 2:
            # 退化：直接用 current_close，与 market 模式一致
            trigger_price = market.current_close
            chosen_level = 0.0
        else:
            # 选 "最近未被突破" 的 fib 档位
            # buy: anchor = swing_high - level × range（往下回撤）；找 close > anchor 的最深档位
            # sell: anchor = swing_low + level × range（往上回撤）；找 close < anchor 的最深档位
            current = market.current_close
            chosen_anchor: float | None = None
            chosen_level = fib_levels[0]
            for level in fib_levels:
                if intent.direction == "buy":
                    candidate = swing_high - level * swing_range
                    # 当前价高于 candidate 才是"未被回撤穿透"的档位（可做支撑）
                    if current > candidate:
                        chosen_anchor = candidate
                        chosen_level = level
                else:
                    candidate = swing_low + level * swing_range
                    if current < candidate:
                        chosen_anchor = candidate
                        chosen_level = level
            if chosen_anchor is None:
                # 全部档位已被穿透 → 用最深档位（0.618 默认）作 anchor
                if intent.direction == "buy":
                    trigger_price = swing_high - fib_levels[-1] * swing_range
                else:
                    trigger_price = swing_low + fib_levels[-1] * swing_range
                chosen_level = fib_levels[-1]
            else:
                trigger_price = chosen_anchor

        zone_half = zone_atr * market.atr_value
        entry_low = trigger_price - zone_half
        entry_high = trigger_price + zone_half

        member = EntrySpecMember(
            member_id="limit_fib_pullback",
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
                "branch": f"fib_{chosen_level}",
                "pattern_type": intent.pattern_type.value,
                "swing_high": round(swing_high, 6),
                "swing_low": round(swing_low, 6),
                "fib_level": chosen_level,
                "lookback_bars": lookback,
            },
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "single_member_limit_fib",
            "params_schema": {
                "fib_levels": "list[float] (default [0.382, 0.5, 0.618])",
                "swing_lookback": "int (default 20)",
                "zone_atr": "float (default 0.10)",
                "validity_bars_<TF>": "int (per-TF, default None=use global)",
            },
            "description": (
                "Fibonacci retracement LIMIT entry: locates recent swing "
                "high/low, places LIMIT at first non-breached fib level."
            ),
        }
