"""Triple-Barrier forward_return（Marcos López de Prado, AFML）。

替代朴素的"入场后 N bar 收盘价退出" forward_return 度量，对每个入场点
模拟三条边界：
  - TP barrier（顺向到 tp_atr × ATR 止盈）
  - SL barrier（逆向到 sl_atr × ATR 止损）
  - Time barrier（到 time_bars 后强退，按该 bar 收盘价）

输出 `BarrierOutcome(barrier, return_pct, bars_held)`，其中 barrier 指明
"先碰到哪个" —— 这个信号对挖掘阶段选择 exit 参数组合至关重要。

参见 `docs/codebase-review.md F-12a`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, NamedTuple, Optional, Sequence


class BarrierOutcome(NamedTuple):
    """三重 barrier 退出结果。

    Attributes:
        barrier:     "tp" / "sl" / "time"（先碰到的那条）
        return_pct:  对数涨跌（入场到退出）小数形式，已扣除往返交易成本
        bars_held:   实际持仓 bar 数（1 ≤ n ≤ time_bars）
    """

    barrier: str
    return_pct: float
    bars_held: int


@dataclass(frozen=True)
class BarrierConfig:
    """Triple-barrier 参数组。

    Attributes:
        sl_atr:    止损距离（× ATR），必须 > 0
        tp_atr:    止盈距离（× ATR），必须 > 0
        time_bars: 时间 barrier（最多持仓 bar 数），必须 ≥ 1
    """

    sl_atr: float
    tp_atr: float
    time_bars: int

    def __post_init__(self) -> None:
        if self.sl_atr <= 0 or self.tp_atr <= 0 or self.time_bars < 1:
            raise ValueError(
                f"BarrierConfig invalid: sl={self.sl_atr}, tp={self.tp_atr}, "
                f"time={self.time_bars}"
            )

    def key(self) -> tuple:
        """供字典索引的不可变 key。"""
        return (
            round(self.sl_atr, 3),
            round(self.tp_atr, 3),
            int(self.time_bars),
        )


# 默认 barrier 扫描网格：覆盖常见 RR 组合
# 保持网格精简（9 组）避免 DataMatrix 体积爆炸
DEFAULT_BARRIER_CONFIGS: tuple[BarrierConfig, ...] = (
    BarrierConfig(sl_atr=1.0, tp_atr=1.5, time_bars=20),
    BarrierConfig(sl_atr=1.0, tp_atr=2.0, time_bars=20),
    BarrierConfig(sl_atr=1.0, tp_atr=3.0, time_bars=40),
    BarrierConfig(sl_atr=1.5, tp_atr=2.0, time_bars=20),
    BarrierConfig(sl_atr=1.5, tp_atr=2.5, time_bars=40),
    BarrierConfig(sl_atr=1.5, tp_atr=3.0, time_bars=40),
    BarrierConfig(sl_atr=2.0, tp_atr=3.0, time_bars=80),
    BarrierConfig(sl_atr=2.0, tp_atr=4.0, time_bars=80),
    BarrierConfig(sl_atr=2.5, tp_atr=5.0, time_bars=120),
)


def _compute_entry_atrs(
    indicators: Sequence[dict],
    atr_indicator: str = "atr14",
    atr_field: str = "atr",
) -> List[Optional[float]]:
    """对齐到 bar index 的 ATR 序列；缺失或非正返回 None。"""
    values: List[Optional[float]] = []
    for snap in indicators:
        atr_block = snap.get(atr_indicator) if isinstance(snap, dict) else None
        raw = atr_block.get(atr_field) if isinstance(atr_block, dict) else None
        if isinstance(raw, (int, float)) and float(raw) > 0:
            values.append(float(raw))
        else:
            values.append(None)
    return values


def _simulate_single_long(
    entry_price: float,
    atr: float,
    config: BarrierConfig,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    entry_idx: int,
    round_trip_cost_pct: float,
) -> Optional[BarrierOutcome]:
    """LONG 方向单次 barrier 模拟。"""
    tp_price = entry_price + config.tp_atr * atr
    sl_price = entry_price - config.sl_atr * atr
    cost = round_trip_cost_pct / 100.0 if round_trip_cost_pct > 0 else 0.0

    last_idx = min(entry_idx + config.time_bars, len(closes) - 1)
    if last_idx <= entry_idx:
        return None

    # 逐 bar 扫描：每根 bar 用 high/low 判断是否触边界。
    # 同一 bar 同时触 TP 和 SL 时保守取 SL（悲观估计）。
    for j in range(entry_idx + 1, last_idx + 1):
        hit_sl = lows[j] <= sl_price
        hit_tp = highs[j] >= tp_price
        if hit_sl and hit_tp:
            return BarrierOutcome("sl", -config.sl_atr * atr / entry_price - cost, j - entry_idx)
        if hit_sl:
            return BarrierOutcome(
                "sl", (sl_price - entry_price) / entry_price - cost, j - entry_idx
            )
        if hit_tp:
            return BarrierOutcome(
                "tp", (tp_price - entry_price) / entry_price - cost, j - entry_idx
            )

    # 时间 barrier：按 last_idx 的 close 退出
    exit_price = closes[last_idx]
    return BarrierOutcome(
        "time", (exit_price - entry_price) / entry_price - cost, last_idx - entry_idx
    )


def _simulate_single_short(
    entry_price: float,
    atr: float,
    config: BarrierConfig,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    entry_idx: int,
    round_trip_cost_pct: float,
) -> Optional[BarrierOutcome]:
    """SHORT 方向单次 barrier 模拟（return 为 "做空收益"：价跌为正）。"""
    tp_price = entry_price - config.tp_atr * atr
    sl_price = entry_price + config.sl_atr * atr
    cost = round_trip_cost_pct / 100.0 if round_trip_cost_pct > 0 else 0.0

    last_idx = min(entry_idx + config.time_bars, len(closes) - 1)
    if last_idx <= entry_idx:
        return None

    for j in range(entry_idx + 1, last_idx + 1):
        hit_sl = highs[j] >= sl_price
        hit_tp = lows[j] <= tp_price
        if hit_sl and hit_tp:
            return BarrierOutcome("sl", -config.sl_atr * atr / entry_price - cost, j - entry_idx)
        if hit_sl:
            return BarrierOutcome(
                "sl", (entry_price - sl_price) / entry_price - cost, j - entry_idx
            )
        if hit_tp:
            return BarrierOutcome(
                "tp", (entry_price - tp_price) / entry_price - cost, j - entry_idx
            )

    exit_price = closes[last_idx]
    return BarrierOutcome(
        "time", (entry_price - exit_price) / entry_price - cost, last_idx - entry_idx
    )


def compute_barrier_returns(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    indicators: Sequence[dict],
    configs: Sequence[BarrierConfig] = DEFAULT_BARRIER_CONFIGS,
    direction: str = "long",
    round_trip_cost_pct: float = 0.0,
    atr_indicator: str = "atr14",
    atr_field: str = "atr",
) -> dict[tuple, List[Optional[BarrierOutcome]]]:
    """对每个 bar 计算多组 barrier 下的 outcome。

    Args:
        opens/highs/lows/closes: 对齐 bar 序列
        indicators: 对齐 bar 的指标快照（用于取 ATR）
        configs:    barrier 参数组合
        direction:  "long" 或 "short"
        round_trip_cost_pct: 往返成本（%），从 return 中扣除
        atr_indicator / atr_field: ATR 取值位置

    Returns:
        {barrier_key: [n_bars 个 Optional[BarrierOutcome]]}
        barrier_key = (sl_atr, tp_atr, time_bars)
        入场价按 "next-bar open" 模型（与 forward_return 一致）。
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction}")

    n = len(opens)
    if not (n == len(highs) == len(lows) == len(closes) == len(indicators)):
        raise ValueError("opens/highs/lows/closes/indicators length mismatch")

    atrs = _compute_entry_atrs(indicators, atr_indicator=atr_indicator, atr_field=atr_field)
    simulate = _simulate_single_long if direction == "long" else _simulate_single_short

    result: dict[tuple, List[Optional[BarrierOutcome]]] = {
        cfg.key(): [None] * n for cfg in configs
    }

    for i in range(n):
        if i + 1 >= n:
            continue
        entry_price = opens[i + 1]
        if entry_price <= 0:
            continue
        # 在信号 bar（i）收盘时读 ATR（与 next-bar open 入场假设相符：
        # 信号在 bar i 产生→bar i+1 开盘入场→ATR 取 i 收盘时的值）
        atr = atrs[i]
        if atr is None:
            continue

        for cfg in configs:
            outcome = simulate(
                entry_price=entry_price,
                atr=atr,
                config=cfg,
                highs=highs,
                lows=lows,
                closes=closes,
                entry_idx=i + 1,  # 入场后第一根 bar 才能被检测
                round_trip_cost_pct=round_trip_cost_pct,
            )
            result[cfg.key()][i] = outcome

    return result
