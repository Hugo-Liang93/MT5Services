"""Weekend/holiday gap mask 测试。

守护 Gap 3（2026-04-22）：
  - 相邻 bar 时间差 > 2×tf_seconds 的位置被判定为 gap
  - 跨 gap 的 forward_return / barrier_return 被 mask 为 None
  - 避免周五→周一 ~50h 空档污染 IC（以及 NFP/FOMC 周末前后的短假期）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.clients.mt5_market import OHLC
from src.research.core.barrier import BarrierOutcome
from src.research.core.data_matrix import (
    _detect_gap_bars,
    _mask_barrier_returns_over_gap,
    _mask_forward_returns_over_gap,
)


def _ohlc(t: datetime, price: float = 2000.0) -> OHLC:
    return OHLC(
        symbol="XAUUSD",
        timeframe="H1",
        time=t,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=100.0,
    )


class TestDetectGapBars:
    def test_continuous_h1_bars_no_gap(self) -> None:
        start = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)  # 周日 10:00
        bars = [_ohlc(start + timedelta(hours=i)) for i in range(5)]
        has_gap = _detect_gap_bars(bars, "H1")
        assert has_gap == [False] * 5

    def test_weekend_gap_detected(self) -> None:
        """模拟周五最后 bar → 周一第一 bar，中间 ~50h 空档。"""
        # 周五 21:00 → 周一 01:00 UTC（约 52 hours）
        fri = datetime(2026, 3, 13, 21, 0, tzinfo=timezone.utc)  # 周五
        mon = datetime(2026, 3, 16, 1, 0, tzinfo=timezone.utc)  # 周一
        bars = [
            _ohlc(fri - timedelta(hours=2)),
            _ohlc(fri - timedelta(hours=1)),
            _ohlc(fri),
            _ohlc(mon),  # 跨周末
            _ohlc(mon + timedelta(hours=1)),
        ]
        has_gap = _detect_gap_bars(bars, "H1")
        # bars[2] 到 bars[3] 间距 52h > 2×3600 → has_gap[2]=True
        assert has_gap == [False, False, True, False, False]

    def test_small_data_hole_below_threshold_not_flagged(self) -> None:
        """broker 偶发 1 bar 空档（间距 2×tf 以内），不标为 gap。"""
        start = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        bars = [
            _ohlc(start),
            _ohlc(start + timedelta(hours=1)),
            # 2 小时后（= 2×tf，刚好卡边界，按 > 2×tf 判定 → 不标）
            _ohlc(start + timedelta(hours=3)),
            _ohlc(start + timedelta(hours=4)),
        ]
        has_gap = _detect_gap_bars(bars, "H1")
        # bars[1] 到 bars[2] 间距 2h，正好 2×tf，不 > → False
        assert has_gap[1] is False


class TestMaskForwardReturns:
    def test_forward_return_over_gap_masked(self) -> None:
        """forward_return[i] 覆盖 [i+1, i+h]，若 has_gap_after[j in [i,i+h-1]] 有 True，mask。"""
        n = 10
        has_gap = [False] * n
        has_gap[4] = True  # bars[4] 到 bars[5] 有 gap
        forward_returns = {
            3: [0.01 * i for i in range(n)],  # 全非 None
        }

        masked = _mask_forward_returns_over_gap(forward_returns, has_gap)

        # h=3，forward_return[i] 持仓 [i+1..i+3]，过渡 j ∈ [i+1, i+2]
        # i=2: j ∈ [3,4] → j=4 True → mask
        # i=3: j ∈ [4,5] → j=4 True → mask
        # i=4: j ∈ [5,6] → no gap → keep（入场已在 gap 之后）
        # i=5: j ∈ [6,7] → no gap → keep
        assert forward_returns[3][1] is not None  # j ∈ [2,3] → no gap
        assert forward_returns[3][2] is None
        assert forward_returns[3][3] is None
        assert forward_returns[3][4] is not None
        assert forward_returns[3][5] is not None
        assert masked == 2

    def test_no_gaps_no_masks(self) -> None:
        n = 10
        has_gap = [False] * n
        forward_returns = {5: [0.01] * n}
        masked = _mask_forward_returns_over_gap(forward_returns, has_gap)
        assert masked == 0
        assert all(r is not None for r in forward_returns[5])

    def test_already_none_not_double_counted(self) -> None:
        n = 5
        has_gap = [False, False, True, False, False]
        forward_returns: dict = {2: [None, None, 0.01, 0.02, 0.03]}
        # h=2: i=1: j ∈ [2,2] → has_gap[2]=True → mask
        # i=1 原本是 None，不计入
        # i=2: j ∈ [3,3] → no gap → keep
        masked = _mask_forward_returns_over_gap(forward_returns, has_gap)
        # 只有原本非 None 的 i=1 本身已是 None，且 h=2 下 i=2 不跨 gap，i=3/4 j 范围 [4]/[5] 无 gap
        # 实际上这个数据集下 masked=0（因为没有非 None 且跨 gap 的位置）
        assert masked == 0


class TestMaskBarrierReturns:
    def test_barrier_over_gap_masked(self) -> None:
        n = 10
        has_gap = [False] * n
        has_gap[5] = True

        # outcome[3] 持仓 5 bar → bars[4..8]，过渡 j ∈ [4,5,6,7] → j=5 True → mask
        # outcome[7] 持仓 2 bar → bars[8..9]，过渡 j ∈ [8] → no gap → keep
        outcomes: List[Optional[BarrierOutcome]] = [None] * n
        outcomes[3] = BarrierOutcome(barrier="tp", return_pct=0.02, bars_held=5)
        outcomes[7] = BarrierOutcome(barrier="sl", return_pct=-0.01, bars_held=2)

        barrier_dict = {(1.5, 2.5, 40): outcomes}
        masked = _mask_barrier_returns_over_gap(barrier_dict, has_gap)

        assert barrier_dict[(1.5, 2.5, 40)][3] is None
        assert barrier_dict[(1.5, 2.5, 40)][7] is not None
        assert masked == 1
