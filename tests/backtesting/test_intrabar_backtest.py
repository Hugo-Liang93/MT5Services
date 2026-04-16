"""Intrabar 回测子循环单元测试。

测试覆盖：
- OHLC 合成纯函数（synthesis.py）
- 子 bar 时间索引构建
- IntrabarContext 初始化
- IntrabarConfig 模型与 from_flat
- 覆盖率检查（low coverage skip）
- Coordinator armed + Guard 去重
- Confirmed 协调（同方向跳过）
- TradeRecord / _Position entry_scope 字段
- Intrabar 关闭时 intrabar 统计输出
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from src.backtesting.engine.intrabar import (
    IntrabarContext,
    build_intrabar_context,
    check_confirmed_coordination,
    intrabar_stats,
)
from src.backtesting.engine.portfolio import _Position
from src.backtesting.models import BacktestConfig, IntrabarConfig, TradeRecord
from src.clients.mt5_market import OHLC
from src.market.synthesis import (
    align_parent_bar_time,
    build_child_bar_index,
    synthesize_parent_bar,
)
from src.signals.orchestration.intrabar_trade_coordinator import (
    IntrabarTradeCoordinator,
    IntrabarTradingPolicy,
)
from src.trading.execution.intrabar_guard import IntrabarTradeGuard

# ── 测试工具 ──────────────────────────────────────────────────────


def _make_bar(
    tf: str = "M5",
    time: datetime | None = None,
    o: float = 2000.0,
    h: float = 2002.0,
    l: float = 1998.0,
    c: float = 2001.0,
    v: float = 100.0,
) -> OHLC:
    return OHLC(
        symbol="XAUUSD",
        timeframe=tf,
        time=time or datetime(2025, 1, 1, tzinfo=timezone.utc),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


def _make_child_bars(
    parent_time: datetime,
    count: int = 6,
    child_tf: str = "M5",
    interval_minutes: int = 5,
    base_price: float = 2000.0,
) -> List[OHLC]:
    """生成属于一根父 bar 的子 TF bars。"""
    bars = []
    for i in range(count):
        t = parent_time + timedelta(minutes=interval_minutes * i)
        p = base_price + i * 0.5
        bars.append(
            _make_bar(
                tf=child_tf,
                time=t,
                o=p,
                h=p + 1.0,
                l=p - 0.5,
                c=p + 0.3,
                v=50.0 + i,
            )
        )
    return bars


# ── synthesis.py 测试 ──────────────────────────────────────────────


class TestSynthesizeParentBar:
    def test_basic_synthesis(self) -> None:
        """基本合成：O=first.open, H=max(highs), L=min(lows), C=last.close, V=sum."""
        parent_time = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        children = _make_child_bars(parent_time, count=3)

        result = synthesize_parent_bar(children, "XAUUSD", "H1", parent_time)

        assert result.symbol == "XAUUSD"
        assert result.timeframe == "H1"
        assert result.time == parent_time
        assert result.open == children[0].open
        assert result.high == max(b.high for b in children)
        assert result.low == min(b.low for b in children)
        assert result.close == children[-1].close
        assert result.volume == sum(b.volume for b in children)

    def test_single_child(self) -> None:
        """单根子 bar 合成。"""
        child = _make_bar(time=datetime(2025, 1, 1, tzinfo=timezone.utc))
        result = synthesize_parent_bar([child], "XAUUSD", "H1", child.time)
        assert result.open == child.open
        assert result.close == child.close

    def test_empty_raises(self) -> None:
        """空输入应 raise。"""
        with pytest.raises(ValueError, match="must not be empty"):
            synthesize_parent_bar(
                [], "XAUUSD", "H1", datetime(2025, 1, 1, tzinfo=timezone.utc)
            )

    def test_incremental_synthesis(self) -> None:
        """增量合成：每次追加一根子 bar，H/L/C 更新。"""
        parent_time = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        children = _make_child_bars(parent_time, count=4)
        accumulated = []

        for i, child in enumerate(children):
            accumulated.append(child)
            result = synthesize_parent_bar(accumulated, "XAUUSD", "H1", parent_time)
            assert result.open == children[0].open  # open 始终是第一根
            assert result.close == child.close  # close 始终是最后一根
            assert result.volume == sum(b.volume for b in accumulated)


class TestAlignParentBarTime:
    def test_h1_alignment(self) -> None:
        """M5 时间对齐到 H1。"""
        child_time = datetime(2025, 1, 1, 14, 35, tzinfo=timezone.utc)
        result = align_parent_bar_time(child_time, "H1")
        assert result == datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)

    def test_m30_alignment(self) -> None:
        """M5 时间对齐到 M30。"""
        child_time = datetime(2025, 1, 1, 14, 17, tzinfo=timezone.utc)
        result = align_parent_bar_time(child_time, "M30")
        assert result == datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)

    def test_exact_boundary(self) -> None:
        """恰好在边界上。"""
        child_time = datetime(2025, 1, 1, 15, 0, tzinfo=timezone.utc)
        result = align_parent_bar_time(child_time, "H1")
        assert result == child_time


class TestBuildChildBarIndex:
    def test_grouping(self) -> None:
        """子 bars 按父 bar 时间正确分组。"""
        t0 = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)

        children_h0 = _make_child_bars(t0, count=12, interval_minutes=5)
        children_h1 = _make_child_bars(t1, count=6, interval_minutes=5)
        all_children = children_h0 + children_h1

        index = build_child_bar_index(all_children, "H1")

        assert t0 in index
        assert t1 in index
        assert len(index[t0]) == 12
        assert len(index[t1]) == 6

    def test_empty_input(self) -> None:
        """空输入返回空 dict。"""
        assert build_child_bar_index([], "H1") == {}


# ── IntrabarConfig 测试 ──────────────────────────────────────────


class TestIntrabarConfig:
    def test_default_disabled(self) -> None:
        """默认 disabled。"""
        cfg = IntrabarConfig()
        assert cfg.enabled is False
        assert cfg.trigger_map == {}

    def test_from_flat_intrabar_fields(self) -> None:
        """from_flat 正确路由 intrabar_ 前缀字段。"""
        config = BacktestConfig.from_flat(
            symbol="XAUUSD",
            timeframe="H1",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
            intrabar_enabled=True,
            intrabar_min_stable_bars=5,
            intrabar_min_confidence=0.80,
            intrabar_confidence_factor=0.90,
            intrabar_min_coverage_ratio=0.70,
        )
        assert config.intrabar.enabled is True
        assert config.intrabar.min_stable_bars == 5
        assert config.intrabar.min_confidence == 0.80
        assert config.intrabar.confidence_factor == 0.90
        assert config.intrabar.min_coverage_ratio == 0.70

    def test_direct_intrabar_config(self) -> None:
        """直接传 IntrabarConfig 对象。"""
        intra = IntrabarConfig(
            enabled=True,
            trigger_map={"H1": "M5"},
            enabled_strategies=["range_reversion"],
        )
        config = BacktestConfig(
            symbol="XAUUSD",
            timeframe="H1",
            start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
            intrabar=intra,
        )
        assert config.intrabar.enabled is True
        assert config.intrabar.trigger_map == {"H1": "M5"}


# ── IntrabarContext 测试 ──────────────────────────────────────────


class TestBuildIntrabarContext:
    def test_basic_build(self) -> None:
        """正常构建 IntrabarContext。"""
        cfg = IntrabarConfig(
            enabled=True,
            trigger_map={"H1": "M5"},
            min_stable_bars=3,
            min_confidence=0.75,
        )
        ctx = build_intrabar_context(cfg, "H1", ["strat_a", "strat_b"])

        assert ctx.child_tf == "M5"
        assert ctx.expected_child_count == 12  # H1=3600s / M5=300s
        assert isinstance(ctx.coordinator, IntrabarTradeCoordinator)
        assert isinstance(ctx.guard, IntrabarTradeGuard)

    def test_missing_trigger_raises(self) -> None:
        """trigger_map 中缺少当前 TF 应 raise。"""
        cfg = IntrabarConfig(enabled=True, trigger_map={"H4": "M15"})
        with pytest.raises(ValueError, match="No trigger mapping"):
            build_intrabar_context(cfg, "H1", ["strat_a"])

    def test_explicit_strategies(self) -> None:
        """显式指定 enabled_strategies 覆盖自动推导。"""
        cfg = IntrabarConfig(
            enabled=True,
            trigger_map={"H1": "M5"},
            enabled_strategies=["only_this"],
        )
        ctx = build_intrabar_context(cfg, "H1", ["strat_a", "strat_b"])
        assert "only_this" in ctx.coordinator.policy.enabled_strategies
        assert "strat_a" not in ctx.coordinator.policy.enabled_strategies


# ── Coordinator + Guard 集成测试 ──────────────────────────────────


class TestCoordinatorGuardIntegration:
    def _make_ctx(self) -> IntrabarContext:
        cfg = IntrabarConfig(
            enabled=True,
            trigger_map={"H1": "M5"},
            min_stable_bars=2,
            min_confidence=0.60,
        )
        return build_intrabar_context(cfg, "H1", ["test_strategy"])

    def test_armed_after_stable_bars(self) -> None:
        """连续 N 根同方向 → armed。"""
        ctx = self._make_ctx()
        bar_time = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)

        # 第 1 根：不 armed
        result = ctx.coordinator.update(
            "XAUUSD", "H1", "test_strategy", "buy", 0.70, bar_time
        )
        assert result is None

        # 第 2 根：armed
        result = ctx.coordinator.update(
            "XAUUSD", "H1", "test_strategy", "buy", 0.70, bar_time
        )
        assert result == "intrabar_armed_buy"

    def test_guard_dedup(self) -> None:
        """同 bar 同策略同方向只能交易一次。"""
        ctx = self._make_ctx()
        bar_time = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)

        # 第一次允许
        allowed, _ = ctx.guard.can_trade(
            "XAUUSD", "H1", "test_strategy", "buy", bar_time
        )
        assert allowed

        # 记录交易
        ctx.guard.record_trade("XAUUSD", "H1", "test_strategy", "buy", bar_time)

        # 第二次阻止
        allowed, reason = ctx.guard.can_trade(
            "XAUUSD", "H1", "test_strategy", "buy", bar_time
        )
        assert not allowed

    def test_confirmed_coordination_same_direction_skip(self) -> None:
        """Confirmed 协调：同方向 intrabar 仓位 → skip。"""
        ctx = self._make_ctx()
        bar_time = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)

        # 模拟 intrabar 已交易
        ctx.guard.record_trade("XAUUSD", "H1", "test_strategy", "buy", bar_time)

        should_skip = check_confirmed_coordination(
            ctx,
            "test_strategy",
            "buy",
            "XAUUSD",
            "H1",
            bar_time,
        )
        assert should_skip is True

    def test_confirmed_coordination_opposite_direction_proceed(self) -> None:
        """Confirmed 协调：反方向 → 不跳过。"""
        ctx = self._make_ctx()
        bar_time = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)

        ctx.guard.record_trade("XAUUSD", "H1", "test_strategy", "buy", bar_time)

        should_skip = check_confirmed_coordination(
            ctx,
            "test_strategy",
            "sell",
            "XAUUSD",
            "H1",
            bar_time,
        )
        assert should_skip is False

    def test_parent_bar_close_resets(self) -> None:
        """父 bar 收盘后状态被清理。"""
        ctx = self._make_ctx()
        bar_time = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)

        ctx.guard.record_trade("XAUUSD", "H1", "test_strategy", "buy", bar_time)
        ctx.coordinator.update("XAUUSD", "H1", "test_strategy", "buy", 0.70, bar_time)

        # 清理
        ctx.coordinator.on_parent_bar_close("XAUUSD", "H1", bar_time)
        ctx.guard.on_parent_bar_close("XAUUSD", "H1", bar_time)

        # 清理后允许交易
        allowed, _ = ctx.guard.can_trade(
            "XAUUSD", "H1", "test_strategy", "buy", bar_time
        )
        assert allowed


# ── IntrabarStats 测试 ────────────────────────────────────────────


class TestIntrabarStats:
    def test_stats_structure(self) -> None:
        """统计输出结构完整。"""
        cfg = IntrabarConfig(enabled=True, trigger_map={"H1": "M5"})
        ctx = build_intrabar_context(cfg, "H1", ["strat_a"])

        stats = intrabar_stats(ctx)
        assert stats["child_tf"] == "M5"
        assert stats["total_intrabar_evaluations"] == 0
        assert stats["total_armed_signals"] == 0
        assert stats["total_intrabar_entries"] == 0
        assert "coordinator" in stats
        assert "guard" in stats


# ── TradeRecord / _Position entry_scope 测试 ──────────────────────


class TestEntryScope:
    def test_trade_record_default_scope(self) -> None:
        """TradeRecord 默认 entry_scope = confirmed。"""
        record = TradeRecord(
            signal_id="bt_test",
            strategy="test",
            direction="buy",
            entry_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            entry_price=2000.0,
            exit_time=datetime(2025, 1, 2, tzinfo=timezone.utc),
            exit_price=2010.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            position_size=0.01,
            pnl=10.0,
            pnl_pct=1.0,
            bars_held=5,
            regime="trending",
            confidence=0.70,
            exit_reason="take_profit",
        )
        assert record.entry_scope == "confirmed"

    def test_trade_record_intrabar_scope(self) -> None:
        """TradeRecord 可设置 entry_scope = intrabar。"""
        record = TradeRecord(
            signal_id="bt_test",
            strategy="test",
            direction="buy",
            entry_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            entry_price=2000.0,
            exit_time=datetime(2025, 1, 2, tzinfo=timezone.utc),
            exit_price=2010.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            position_size=0.01,
            pnl=10.0,
            pnl_pct=1.0,
            bars_held=5,
            regime="trending",
            confidence=0.70,
            exit_reason="take_profit",
            entry_scope="intrabar",
        )
        assert record.entry_scope == "intrabar"

    def test_position_entry_scope(self) -> None:
        """_Position 支持 entry_scope 字段。"""
        pos = _Position(
            signal_id="bt_test",
            strategy="test",
            direction="buy",
            entry_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            position_size=0.01,
            regime="trending",
            confidence=0.70,
            entry_bar_index=0,
            entry_scope="intrabar",
        )
        assert pos.entry_scope == "intrabar"
