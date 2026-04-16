"""单笔交易亏损 hard cap 契约测试。"""

from __future__ import annotations

import pytest

from src.trading.execution.risk_caps import (
    MaxSingleTradeLossPolicy,
    SingleTradeLossCheckResult,
    check_single_trade_loss_cap,
)


class TestPolicyContract:
    def test_disabled(self) -> None:
        p = MaxSingleTradeLossPolicy(max_loss_usd=None)
        assert not p.enabled()

    def test_enabled_positive(self) -> None:
        p = MaxSingleTradeLossPolicy(max_loss_usd=100.0)
        assert p.enabled()

    def test_zero_hard_stop(self) -> None:
        """0.0 是合法值 —— 紧急停手（任何新仓都被拒）。"""
        p = MaxSingleTradeLossPolicy(max_loss_usd=0.0)
        assert p.enabled()

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            MaxSingleTradeLossPolicy(max_loss_usd=-10.0)


class TestCheckCap:
    def test_disabled_policy_always_allows(self) -> None:
        policy = MaxSingleTradeLossPolicy(max_loss_usd=None)
        r = check_single_trade_loss_cap(
            policy, position_size=10.0, sl_distance=100.0, contract_size=100.0
        )
        assert r.allowed is True
        assert r.max_allowed_usd is None
        assert r.reason is None

    def test_within_cap(self) -> None:
        # loss = 15.0 × 0.1 × 100 = $150，cap=$200
        policy = MaxSingleTradeLossPolicy(max_loss_usd=200.0)
        r = check_single_trade_loss_cap(
            policy, position_size=0.1, sl_distance=15.0, contract_size=100.0
        )
        assert r.allowed is True
        assert r.estimated_loss_usd == pytest.approx(150.0)
        assert r.max_allowed_usd == 200.0

    def test_exceeds_cap(self) -> None:
        # loss = 30.0 × 0.1 × 100 = $300，cap=$200 → 拒
        policy = MaxSingleTradeLossPolicy(max_loss_usd=200.0)
        r = check_single_trade_loss_cap(
            policy, position_size=0.1, sl_distance=30.0, contract_size=100.0
        )
        assert r.allowed is False
        assert r.estimated_loss_usd == pytest.approx(300.0)
        assert r.reason is not None
        assert "300.00" in r.reason

    def test_zero_cap_blocks_all(self) -> None:
        policy = MaxSingleTradeLossPolicy(max_loss_usd=0.0)
        r = check_single_trade_loss_cap(
            policy, position_size=0.01, sl_distance=1.0, contract_size=100.0
        )
        # estimated = 1 × 0.01 × 100 = $1 > $0 cap
        assert r.allowed is False

    def test_negative_inputs_raise(self) -> None:
        policy = MaxSingleTradeLossPolicy(max_loss_usd=100.0)
        with pytest.raises(ValueError):
            check_single_trade_loss_cap(
                policy, position_size=-0.1, sl_distance=10.0, contract_size=100.0
            )
        with pytest.raises(ValueError):
            check_single_trade_loss_cap(
                policy, position_size=0.1, sl_distance=-10.0, contract_size=100.0
            )
