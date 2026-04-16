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

import numpy as np


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
    """对每个 bar 计算多组 barrier 下的 outcome（向量化 NumPy 实现）。

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

    实现说明：
        每个 config 用一次 vectorized 扫描替代 Python 内层循环。同 bar 同时触发
        SL/TP 时保守取 SL（与 per-bar 旧实现一致）。
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction}")

    n = len(opens)
    if not (n == len(highs) == len(lows) == len(closes) == len(indicators)):
        raise ValueError("opens/highs/lows/closes/indicators length mismatch")

    atrs = _compute_entry_atrs(
        indicators, atr_indicator=atr_indicator, atr_field=atr_field
    )

    # 将所有输入转为 ndarray，一次性完成
    opens_arr = np.asarray(opens, dtype=np.float64)
    highs_arr = np.asarray(highs, dtype=np.float64)
    lows_arr = np.asarray(lows, dtype=np.float64)
    closes_arr = np.asarray(closes, dtype=np.float64)
    atrs_arr = np.asarray(
        [a if a is not None else np.nan for a in atrs], dtype=np.float64,
    )

    result: dict[tuple, List[Optional[BarrierOutcome]]] = {
        cfg.key(): [None] * n for cfg in configs
    }

    for cfg in configs:
        outcomes = _vectorized_barrier_scan(
            opens=opens_arr,
            highs=highs_arr,
            lows=lows_arr,
            closes=closes_arr,
            atrs=atrs_arr,
            config=cfg,
            direction=direction,
            round_trip_cost_pct=round_trip_cost_pct,
        )
        result[cfg.key()] = outcomes

    return result


def _vectorized_barrier_scan(
    *,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    atrs: np.ndarray,
    config: BarrierConfig,
    direction: str,
    round_trip_cost_pct: float,
) -> List[Optional[BarrierOutcome]]:
    """向量化单 config 的三 barrier 扫描。

    对每个有效 entry 构建 time_bars 宽的窗口矩阵，用 np.argmax 找首次触发。

    算法：
        1. 对每个 i，entry_idx = i+1, entry_price = opens[i+1]
        2. 窗口 = highs[i+1 : i+1+time_bars]（类似 lows/closes）
        3. tp_hit[j] = (高点 >= tp_price)，sl_hit[j] = (低点 <= sl_price)
        4. first_tp / first_sl = argmax(hit) 若 any(hit) else inf
        5. 比较先后：sl 优先（同 tick 保守）

    时间复杂度：O(n × time_bars)，内存 O(n × time_bars)。
    """
    n = len(opens)
    out: List[Optional[BarrierOutcome]] = [None] * n
    if n < 2:
        return out

    cost = round_trip_cost_pct / 100.0 if round_trip_cost_pct > 0 else 0.0
    time_bars = int(config.time_bars)

    # entry_idx = i+1（入场 bar）；扫描范围 [entry_idx+1, entry_idx+time_bars] = [i+2, i+1+time_bars]
    # 为保持索引一致，对 highs/lows/closes 末尾 pad (time_bars+1) 个 NaN。
    pad_h = np.concatenate([highs, np.full(time_bars + 1, np.nan)])
    pad_l = np.concatenate([lows, np.full(time_bars + 1, np.nan)])
    pad_c = np.concatenate([closes, np.full(time_bars + 1, np.nan)])

    # 窗口矩阵：shape (n-1, time_bars)。row i 对应扫描 bar [i+2 .. i+1+time_bars]
    idx_matrix = (np.arange(n - 1)[:, None] + 2 + np.arange(time_bars)[None, :])
    # 越界位置由 pad 的 NaN 填充
    win_h = pad_h[idx_matrix]
    win_l = pad_l[idx_matrix]
    win_c = pad_c[idx_matrix]

    entry_prices = opens[1:]  # shape (n-1,)
    entry_atrs = atrs[:-1]    # 信号 bar 的 ATR（与 per-bar 实现一致）

    valid = (entry_prices > 0) & np.isfinite(entry_atrs)

    if direction == "long":
        tp_prices = entry_prices + config.tp_atr * entry_atrs
        sl_prices = entry_prices - config.sl_atr * entry_atrs
        tp_hit = win_h >= tp_prices[:, None]
        sl_hit = win_l <= sl_prices[:, None]
    else:  # short
        tp_prices = entry_prices - config.tp_atr * entry_atrs
        sl_prices = entry_prices + config.sl_atr * entry_atrs
        tp_hit = win_l <= tp_prices[:, None]
        sl_hit = win_h >= sl_prices[:, None]

    # NaN 位置（越界）既不算 tp 也不算 sl（broadcast 下 NaN 比较结果为 False）
    # 但需防止 argmax 在全 False 时返回 0 误判为"首 bar 命中"
    any_tp = tp_hit.any(axis=1)
    any_sl = sl_hit.any(axis=1)
    first_tp = np.where(any_tp, tp_hit.argmax(axis=1), time_bars)
    first_sl = np.where(any_sl, sl_hit.argmax(axis=1), time_bars)

    # 同 bar 双触 → SL 获胜（保守）；sl 严格先 → SL；tp 严格先 → TP；都没命中 → TIME
    # time_bars 是"哨兵"值（未命中）
    sl_wins = any_sl & (first_sl <= first_tp)
    tp_wins = any_tp & (first_tp < first_sl)
    time_exit = (~any_sl) & (~any_tp)

    # 构造 outcome（Python 层，因为要实例化 NamedTuple 和处理 None）
    for i in range(n - 1):
        if not valid[i]:
            continue

        if sl_wins[i]:
            if direction == "long":
                ret = -config.sl_atr * entry_atrs[i] / entry_prices[i] - cost
            else:
                ret = -config.sl_atr * entry_atrs[i] / entry_prices[i] - cost
            out[i] = BarrierOutcome("sl", float(ret), int(first_sl[i] + 1))
        elif tp_wins[i]:
            tp_price = tp_prices[i]
            if direction == "long":
                ret = (tp_price - entry_prices[i]) / entry_prices[i] - cost
            else:
                ret = (entry_prices[i] - tp_price) / entry_prices[i] - cost
            out[i] = BarrierOutcome("tp", float(ret), int(first_tp[i] + 1))
        elif time_exit[i]:
            # 时间 barrier：按 last_idx 的 close 退出（如 NaN 则无效）
            last_idx_offset = time_bars - 1
            last_close = win_c[i, last_idx_offset]
            if not np.isfinite(last_close):
                # 窗口不足 time_bars 根 → 数据边界，无有效 outcome
                continue
            if direction == "long":
                ret = (last_close - entry_prices[i]) / entry_prices[i] - cost
            else:
                ret = (entry_prices[i] - last_close) / entry_prices[i] - cost
            out[i] = BarrierOutcome("time", float(ret), int(time_bars))
    return out
