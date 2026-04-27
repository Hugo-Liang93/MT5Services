"""execution_semantics.resolve_trade_parameters 单元测试。

覆盖 SimulationMode.EXECUTION_FEASIBILITY 下的两个语义：
1. 默认 fallback 关闭 → raw < min_volume 时拒（保留原有 broker semantic）
2. fallback 启用 → 在实际风险占比 ≤ max_actual_risk_pct 时强制使用 min_volume；
   超过上限继续拒（避免小账户大 SL 单笔吃掉资金）

2026-04-27 评估发现 $2000 + XAUUSD contract_size=100 + risk%=1 物理上多数情况
产 sub-min-volume；用户希望保留 $2000 账户但能下 0.01 最小手数。fallback 是
显式的"主动放弃 risk discipline 换取执行可能"，必须配上限保护。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtesting.engine.execution_semantics import resolve_trade_parameters
from src.backtesting.models import BacktestConfig, PositionConfig, SimulationMode
from src.trading.execution.sizing import TradeParameters


def _make_config(
    *,
    simulation_mode: SimulationMode = SimulationMode.EXECUTION_FEASIBILITY,
    allow_min_volume_fallback: bool = False,
    max_actual_risk_pct: float = 5.0,
    risk_percent: float = 1.0,
    min_volume: float = 0.01,
    contract_size: float = 100.0,
    timeframe: str = "H1",
) -> BacktestConfig:
    return BacktestConfig(
        symbol="XAUUSD",
        timeframe=timeframe,
        start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2025, 2, 1, tzinfo=timezone.utc),
        simulation_mode=simulation_mode,
        position=PositionConfig(
            contract_size=contract_size,
            risk_percent=risk_percent,
            min_volume=min_volume,
            max_volume=1.0,
            allow_min_volume_fallback=allow_min_volume_fallback,
            max_actual_risk_pct=max_actual_risk_pct,
        ),
    )


def _make_trade_params(*, sl_distance: float) -> TradeParameters:
    """构造合法 TradeParameters；resolve_trade_parameters 只读 sl_distance + position_size。"""
    return TradeParameters(
        entry_price=2000.0,
        stop_loss=2000.0 - sl_distance,
        take_profit=2000.0 + sl_distance * 2,
        position_size=0.0,
        risk_reward_ratio=2.0,
        atr_value=sl_distance / 2,
        sl_distance=sl_distance,
        tp_distance=sl_distance * 2,
    )


# ── EXECUTION_FEASIBILITY 默认 fallback OFF（保留原 broker semantic）──


def test_ef_default_rejects_sub_min_volume() -> None:
    """默认 fallback OFF：$2000 + sl=30 → raw=0.0067 < 0.01 → 拒。"""
    config = _make_config(allow_min_volume_fallback=False)
    trade_params = _make_trade_params(sl_distance=30.0)

    resolution = resolve_trade_parameters(config, trade_params, account_balance=2000.0)

    assert resolution.accepted is False
    assert resolution.trade_params is None
    assert resolution.reason == "below_min_volume_for_execution_feasibility"


def test_ef_default_accepts_when_raw_above_min_volume() -> None:
    """raw ≥ min_volume 时正常走 (sl=20, balance=$2000, risk=1% → raw=0.01)。"""
    config = _make_config(allow_min_volume_fallback=False)
    trade_params = _make_trade_params(sl_distance=20.0)

    resolution = resolve_trade_parameters(config, trade_params, account_balance=2000.0)

    assert resolution.accepted is True
    assert resolution.trade_params is not None
    assert resolution.resolved_position_size == pytest.approx(0.01)


# ── EXECUTION_FEASIBILITY fallback ON（用户显式接受高 risk%）──


def test_ef_fallback_on_promotes_to_min_volume_within_risk_cap() -> None:
    """fallback ON + sl=30 → raw=0.0067 < 0.01 → 强制 0.01；实际 risk = $30 = 1.5% < 5% cap → accept。"""
    config = _make_config(
        allow_min_volume_fallback=True,
        max_actual_risk_pct=5.0,
    )
    trade_params = _make_trade_params(sl_distance=30.0)

    resolution = resolve_trade_parameters(config, trade_params, account_balance=2000.0)

    assert resolution.accepted is True
    assert resolution.trade_params is not None
    # min_volume = 0.01
    assert resolution.resolved_position_size == pytest.approx(0.01)
    # raw 仍然记录（< 0.01），方便审计
    assert resolution.raw_position_size < 0.01


def test_ef_fallback_on_rejects_when_actual_risk_exceeds_cap() -> None:
    """fallback ON + sl=200 → 0.01 lot 实际风险 = $200 = 10% > 5% cap → 拒。"""
    config = _make_config(
        allow_min_volume_fallback=True,
        max_actual_risk_pct=5.0,
    )
    trade_params = _make_trade_params(sl_distance=200.0)

    resolution = resolve_trade_parameters(config, trade_params, account_balance=2000.0)

    assert resolution.accepted is False
    assert resolution.trade_params is None
    assert resolution.reason == "exceeds_max_actual_risk_pct"


def test_ef_fallback_on_at_exact_risk_cap_accepts() -> None:
    """实际 risk = cap 边界 (0.01 × 100 × 100 = $100 = 5% of $2000) → accept。"""
    config = _make_config(
        allow_min_volume_fallback=True,
        max_actual_risk_pct=5.0,
    )
    trade_params = _make_trade_params(sl_distance=100.0)

    resolution = resolve_trade_parameters(config, trade_params, account_balance=2000.0)

    assert resolution.accepted is True
    assert resolution.resolved_position_size == pytest.approx(0.01)


def test_ef_fallback_irrelevant_when_raw_above_min_volume() -> None:
    """fallback ON 但 raw 已 ≥ min_volume：走正常路径，不触发 fallback。"""
    config = _make_config(
        allow_min_volume_fallback=True,
        max_actual_risk_pct=5.0,
    )
    trade_params = _make_trade_params(sl_distance=15.0)

    resolution = resolve_trade_parameters(config, trade_params, account_balance=2000.0)

    assert resolution.accepted is True
    # raw ≈ 0.0133 → floor 到 0.01
    assert resolution.resolved_position_size == pytest.approx(0.01)


# ── RESEARCH 模式不受影响 ──


def test_research_mode_unaffected_by_fallback_flag() -> None:
    """RESEARCH 模式仍允许 fractional lot，与 fallback flag 无关。"""
    config = _make_config(
        simulation_mode=SimulationMode.RESEARCH,
        allow_min_volume_fallback=False,
    )
    trade_params = _make_trade_params(sl_distance=30.0)

    resolution = resolve_trade_parameters(config, trade_params, account_balance=2000.0)

    assert resolution.accepted is True
    # raw = 0.0067 直接通过（RESEARCH 不做 floor）
    assert resolution.resolved_position_size == pytest.approx(0.00666666, abs=1e-6)
