"""ADR-013 P4-residual #1: 回测 OCO group 多 member 触发与 tie-break 决定论。

覆盖：
- A 单触发 / B 单触发 / 都不触发
- 同 bar 双触发 → tie_break = limit_first / stop_first / alpha
- 一个 member fill → 整 group 移除
- expiry → 整 group 标 expired
- 单 member 路径（OCO 退化）仍工作

核心是验证 `check_pending_entries` 的状态机决定论：相同输入相同 winner，
与 live ``_on_member_filled`` 任一成交撤其余 sibling 语义对齐。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from src.backtesting.engine.pending_state import (
    BacktestPendingGroup,
    BacktestPendingMember,
)
from src.backtesting.engine.signals import (
    _select_tie_break_winner,
    check_pending_entries,
)
from src.clients.mt5_market import OHLC
from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalDecision


def _make_bar(o: float, h: float, low: float, c: float) -> OHLC:
    return OHLC(
        symbol="XAUUSD",
        timeframe="M15",
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=0,
    )


def _make_decision(direction: str = "buy") -> SignalDecision:
    return SignalDecision(
        strategy="structured_price_action",
        symbol="XAUUSD",
        timeframe="M15",
        direction=direction,
        confidence=0.7,
        reason="test_oco",
    )


def _make_oco_group(
    decision: SignalDecision,
    *,
    expiry_bar: int,
    limit_low: float = 1990.0,
    limit_high: float = 1995.0,
    stop_low: float = 2010.0,
    stop_high: float = 2015.0,
) -> BacktestPendingGroup:
    """构造 OCO group：buy → limit 在下方回踩，stop 在上方突破。"""
    return BacktestPendingGroup(
        decision=decision,
        members=[
            BacktestPendingMember(
                member_id="limit_pullback",
                entry_type="limit",
                entry_low=limit_low,
                entry_high=limit_high,
            ),
            BacktestPendingMember(
                member_id="stop_breakout",
                entry_type="stop",
                entry_low=stop_low,
                entry_high=stop_high,
            ),
        ],
        expiry_bar=expiry_bar,
    )


class _FakeRegistry:
    def __init__(self, tie_break: str = "limit_first") -> None:
        self.fill_semantics_tie_break = tie_break


class _FakeEngine:
    """只提供 check_pending_entries 直接依赖的字段。"""

    def __init__(self, tie_break: str = "limit_first") -> None:
        self._pending_groups: Dict[str, BacktestPendingGroup] = {}
        self._entry_policy_registry = _FakeRegistry(tie_break)


@pytest.fixture
def stubbed_execute_entry(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """记录 execute_entry 调用参数，避开 portfolio/sizing 依赖。"""
    calls: List[Dict[str, Any]] = []

    def _recorder(
        engine: Any,
        decision: SignalDecision,
        bar: OHLC,
        bar_index: int,
        atr_value: float,
        regime: RegimeType,
        indicators: Any = None,
        fill_price: float | None = None,
        entry_scope: str = "confirmed",
    ) -> None:
        calls.append(
            {
                "decision": decision,
                "bar_index": bar_index,
                "atr_value": atr_value,
                "fill_price": fill_price,
            }
        )

    monkeypatch.setattr("src.backtesting.engine.signals.execute_entry", _recorder)
    return calls


# ── _select_tie_break_winner 纯单元 ─────────────────────────────────────────


class TestSelectTieBreakWinner:
    def test_single_trigger_returns_unchanged(self) -> None:
        triggers = [("limit_pullback", 1995.0, "limit")]
        assert _select_tie_break_winner(triggers, "limit_first") == triggers[0]

    def test_limit_first_picks_limit_when_both_trigger(self) -> None:
        triggers = [
            ("stop_breakout", 2010.0, "stop"),
            ("limit_pullback", 1995.0, "limit"),
        ]
        winner = _select_tie_break_winner(triggers, "limit_first")
        assert winner[0] == "limit_pullback"

    def test_stop_first_picks_stop_when_both_trigger(self) -> None:
        triggers = [
            ("limit_pullback", 1995.0, "limit"),
            ("stop_breakout", 2010.0, "stop"),
        ]
        winner = _select_tie_break_winner(triggers, "stop_first")
        assert winner[0] == "stop_breakout"

    def test_alpha_picks_lexicographic_minimum(self) -> None:
        triggers = [
            ("z_member", 2010.0, "stop"),
            ("a_member", 1995.0, "limit"),
        ]
        winner = _select_tie_break_winner(triggers, "alpha")
        assert winner[0] == "a_member"

    def test_limit_first_falls_back_to_alpha_when_no_limit(self) -> None:
        # 全 stop → 走 alpha 兜底，决定论
        triggers = [
            ("stop_b", 2015.0, "stop"),
            ("stop_a", 2010.0, "stop"),
        ]
        winner = _select_tie_break_winner(triggers, "limit_first")
        assert winner[0] == "stop_a"

    def test_stop_first_falls_back_to_alpha_when_no_stop(self) -> None:
        triggers = [
            ("limit_b", 1995.0, "limit"),
            ("limit_a", 1990.0, "limit"),
        ]
        winner = _select_tie_break_winner(triggers, "stop_first")
        assert winner[0] == "limit_a"


# ── check_pending_entries OCO 行为 ─────────────────────────────────────────


class TestOcoPendingGroup:
    def test_only_limit_triggers_executes_limit_and_removes_group(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        engine = _FakeEngine()
        decision = _make_decision()
        group = _make_oco_group(decision, expiry_bar=10)
        engine._pending_groups["g1"] = group

        # bar 跌入 limit zone（1990-1995），未达 stop（2010+）
        bar = _make_bar(2000, 2002, 1993, 1998)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=2,
            indicators={"atr14": {"atr": 5.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" not in engine._pending_groups
        assert len(stubbed_execute_entry) == 1
        assert stubbed_execute_entry[0]["fill_price"] == 1995.0
        assert group.status == "filled"
        assert group.members[0].status == "filled"
        assert group.members[1].status == "cancelled"

    def test_only_stop_triggers_executes_stop_and_removes_group(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        engine = _FakeEngine()
        decision = _make_decision()
        group = _make_oco_group(decision, expiry_bar=10)
        engine._pending_groups["g1"] = group

        # bar 突破 stop zone（2010-2015），未触 limit
        bar = _make_bar(2005, 2012, 2003, 2011)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=2,
            indicators={"atr14": {"atr": 5.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" not in engine._pending_groups
        assert len(stubbed_execute_entry) == 1
        # buy stop trigger 在 entry_low=2010 成交
        assert stubbed_execute_entry[0]["fill_price"] == 2010.0
        assert group.members[0].status == "cancelled"
        assert group.members[1].status == "filled"

    def test_neither_member_triggers_keeps_group(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        engine = _FakeEngine()
        decision = _make_decision()
        group = _make_oco_group(decision, expiry_bar=10)
        engine._pending_groups["g1"] = group

        # bar 在 zone 之间游荡（1996-2008）→ 都不触
        bar = _make_bar(2000, 2008, 1996, 2002)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=2,
            indicators={"atr14": {"atr": 5.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" in engine._pending_groups
        assert stubbed_execute_entry == []
        # 两个 member 都还在 active
        for member in group.members:
            assert member.status == "active"

    def test_both_triggered_in_same_bar_limit_first_wins(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        engine = _FakeEngine(tie_break="limit_first")
        decision = _make_decision()
        group = _make_oco_group(decision, expiry_bar=10)
        engine._pending_groups["g1"] = group

        # bar 同时涵盖 limit zone 和 stop zone
        bar = _make_bar(2000, 2020, 1985, 2018)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=2,
            indicators={"atr14": {"atr": 5.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" not in engine._pending_groups
        assert len(stubbed_execute_entry) == 1
        # limit_first → limit member 成交（fill_price = entry_high 1995 或 open
        # 取最小，open=2000 > 1995 → 1995）
        assert stubbed_execute_entry[0]["fill_price"] == 1995.0
        assert group.members[0].status == "filled"
        assert group.members[1].status == "cancelled"

    def test_both_triggered_in_same_bar_stop_first_wins(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        engine = _FakeEngine(tie_break="stop_first")
        decision = _make_decision()
        group = _make_oco_group(decision, expiry_bar=10)
        engine._pending_groups["g1"] = group

        bar = _make_bar(2000, 2020, 1985, 2018)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=2,
            indicators={"atr14": {"atr": 5.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" not in engine._pending_groups
        assert len(stubbed_execute_entry) == 1
        # buy stop trigger 在 entry_low=2010 成交
        assert stubbed_execute_entry[0]["fill_price"] == 2010.0
        assert group.members[0].status == "cancelled"
        assert group.members[1].status == "filled"

    def test_expiry_marks_group_expired_and_removes(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        engine = _FakeEngine()
        decision = _make_decision()
        group = _make_oco_group(decision, expiry_bar=5)
        engine._pending_groups["g1"] = group

        # bar_index 超过 expiry，且 bar 也触发 limit zone（验证 expiry 优先）
        bar = _make_bar(2000, 2002, 1993, 1998)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=6,
            indicators={"atr14": {"atr": 5.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" not in engine._pending_groups
        assert stubbed_execute_entry == []
        assert group.status == "expired"
        for member in group.members:
            assert member.status == "expired"

    def test_single_member_group_still_works(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        """OCO 退化为单 member 路径同样要工作（向后兼容）。"""
        engine = _FakeEngine()
        decision = _make_decision()
        group = BacktestPendingGroup(
            decision=decision,
            members=[
                BacktestPendingMember(
                    member_id="market",
                    entry_type="limit",
                    entry_low=1990.0,
                    entry_high=1995.0,
                ),
            ],
            expiry_bar=10,
        )
        engine._pending_groups["g1"] = group

        bar = _make_bar(2000, 2002, 1993, 1998)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=2,
            indicators={"atr14": {"atr": 5.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" not in engine._pending_groups
        assert len(stubbed_execute_entry) == 1
        assert stubbed_execute_entry[0]["fill_price"] == 1995.0

    def test_skip_execute_when_atr_zero(
        self, stubbed_execute_entry: List[Dict[str, Any]]
    ) -> None:
        """ATR=0 时不调 execute_entry，但仍按 OCO 语义移除整 group。"""
        engine = _FakeEngine()
        decision = _make_decision()
        group = _make_oco_group(decision, expiry_bar=10)
        engine._pending_groups["g1"] = group

        bar = _make_bar(2000, 2002, 1993, 1998)
        check_pending_entries(
            engine,  # type: ignore[arg-type]
            bar,
            bar_index=2,
            indicators={"atr14": {"atr": 0.0}},
            regime=RegimeType.TRENDING,
        )

        assert "g1" not in engine._pending_groups
        assert stubbed_execute_entry == []
        # OCO 语义：触发但 execute 失败仍要 cancel sibling
        assert group.members[0].status == "filled"
        assert group.members[1].status == "cancelled"
