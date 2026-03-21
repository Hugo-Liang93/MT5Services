"""tests/signals/test_voting.py — StrategyVotingEngine 单元测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.signals.models import SignalDecision
from src.signals.evaluation.regime import RegimeType
from src.signals.orchestration import StrategyVotingEngine


# ── 辅助工厂 ─────────────────────────────────────────────────────────────────

def _decision(
    action: str,
    confidence: float = 0.70,
    strategy: str = "test",
    symbol: str = "XAUUSD",
    timeframe: str = "M1",
    used_indicators: list[str] | None = None,
) -> SignalDecision:
    return SignalDecision(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        action=action,
        confidence=confidence,
        reason="test",
        used_indicators=used_indicators or [],
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


DEFAULT_REGIME = RegimeType.TRENDING
DEFAULT_SCOPE = "confirmed"


# ── 基础表决 ─────────────────────────────────────────────────────────────────

class TestBasicVoting:
    def test_empty_decisions_returns_none(self):
        engine = StrategyVotingEngine()
        assert engine.vote([], regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE) is None

    def test_all_hold_returns_none(self):
        engine = StrategyVotingEngine(min_quorum=1)
        decisions = [_decision("hold"), _decision("hold")]
        assert engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE) is None

    def test_single_buy_above_threshold(self):
        engine = StrategyVotingEngine(consensus_threshold=0.40, min_quorum=1)
        # 1 个 buy，无 sell/hold → buy_score = 1.0 >= 0.40
        result = engine.vote(
            [_decision("buy", 0.8)],
            regime=DEFAULT_REGIME,
            scope=DEFAULT_SCOPE,
        )
        assert result is not None
        assert result.action == "buy"

    def test_single_sell_above_threshold(self):
        engine = StrategyVotingEngine(consensus_threshold=0.40, min_quorum=1)
        result = engine.vote(
            [_decision("sell", 0.8)],
            regime=DEFAULT_REGIME,
            scope=DEFAULT_SCOPE,
        )
        assert result is not None
        assert result.action == "sell"

    def test_strategy_field_is_consensus(self):
        engine = StrategyVotingEngine(min_quorum=1)
        result = engine.vote(
            [_decision("buy", 0.9)],
            regime=DEFAULT_REGIME,
            scope=DEFAULT_SCOPE,
        )
        assert result is not None
        assert result.strategy == "consensus"


# ── 法定人数检查 ──────────────────────────────────────────────────────────────

class TestQuorum:
    def test_below_min_quorum_returns_none(self):
        engine = StrategyVotingEngine(min_quorum=2)
        # 只有 1 个非 hold 策略 → 低于 quorum
        decisions = [_decision("buy", 0.9), _decision("hold", 0.5)]
        assert engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE) is None

    def test_exactly_min_quorum(self):
        engine = StrategyVotingEngine(min_quorum=2, consensus_threshold=0.40)
        decisions = [_decision("buy", 0.8, "s1"), _decision("buy", 0.7, "s2")]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        assert result is not None
        assert result.action == "buy"

    def test_quorum_counts_sell_as_non_hold(self):
        engine = StrategyVotingEngine(min_quorum=2, consensus_threshold=0.40)
        # 1 buy + 1 sell = 2 非 hold，达到 quorum
        decisions = [_decision("buy", 0.8, "s1"), _decision("sell", 0.3, "s2")]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        # buy_score = 0.8/(0.8+0.3) ≈ 0.727 >= 0.40 且 > sell_score
        assert result is not None
        assert result.action == "buy"


# ── 置信度计算 ───────────────────────────────────────────────────────────────

class TestConfidenceCalculation:
    def test_pure_buy_consensus_confidence(self):
        """无 sell 方向 → disagreement_factor=0 → confidence = buy_score。"""
        engine = StrategyVotingEngine(
            consensus_threshold=0.40, min_quorum=1, disagreement_penalty=0.50
        )
        # buy_weight = 0.6 + 0.4 = 1.0, hold_weight = 0, sell_weight = 0
        # buy_score = 1.0, sell_score = 0.0
        # min_side = 0 → disagreement_factor = 0
        # consensus_confidence = 1.0 * (1 - 0) = 1.0
        decisions = [_decision("buy", 0.6, "s1"), _decision("buy", 0.4, "s2")]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        assert result is not None
        assert result.confidence == pytest.approx(1.0)

    def test_disagreement_reduces_confidence(self):
        """买卖各半 → disagreement_factor=0.5*penalty → confidence 下降。"""
        engine = StrategyVotingEngine(
            consensus_threshold=0.40, min_quorum=2, disagreement_penalty=0.50
        )
        # buy=0.5, sell=0.5 → buy_score=0.5, sell_score=0.5 → 方向相同？
        # sell_score = buy_score → 两者相等，条件 buy_score > sell_score 不满足 → None
        decisions = [_decision("buy", 0.5, "s1"), _decision("sell", 0.5, "s2")]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        # 完全相等 → 不产生共识
        assert result is None

    def test_weak_disagreement_partially_reduces_confidence(self):
        """部分分歧时置信度有所降低但不为零。"""
        engine = StrategyVotingEngine(
            consensus_threshold=0.40, min_quorum=2, disagreement_penalty=0.50
        )
        # buy=0.8, sell=0.2 → total=1.0
        # buy_score=0.8, sell_score=0.2
        # min_side=0.2, raw_disagreement=0.2/0.5=0.4
        # disagreement_factor = (0.4^2) * 0.5 = 0.08 (平方衰减，轻分歧惩罚更轻)
        # confidence = 0.8 * (1 - 0.08) = 0.736
        decisions = [_decision("buy", 0.8, "s1"), _decision("sell", 0.2, "s2")]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        assert result is not None
        assert result.action == "buy"
        assert result.confidence == pytest.approx(0.736)

    def test_hold_weight_reduces_direction_scores(self):
        """hold 权重加入总权重后，方向得分下降。"""
        engine = StrategyVotingEngine(
            consensus_threshold=0.40, min_quorum=1, disagreement_penalty=0.50
        )
        # buy=0.6, hold=0.4 → total=1.0
        # buy_score = 0.6, sell_score = 0
        # confidence = 0.6
        decisions = [_decision("buy", 0.6, "s1"), _decision("hold", 0.4, "s2")]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        assert result is not None
        assert result.confidence == pytest.approx(0.6)


# ── 阈值过滤 ─────────────────────────────────────────────────────────────────

class TestThresholdFilter:
    def test_buy_below_threshold_returns_none(self):
        engine = StrategyVotingEngine(consensus_threshold=0.60, min_quorum=1)
        # buy=0.5, hold=0.5 → buy_score=0.5 < 0.6
        decisions = [_decision("buy", 0.5, "s1"), _decision("hold", 0.5, "s2")]
        assert engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE) is None

    def test_buy_exactly_at_threshold(self):
        engine = StrategyVotingEngine(consensus_threshold=0.60, min_quorum=1)
        # buy=0.6, sell=0.0, hold=0.4 → buy_score = 0.6/1.0 = 0.6 >= 0.6
        decisions = [_decision("buy", 0.6, "s1"), _decision("hold", 0.4, "s2")]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        assert result is not None
        assert result.action == "buy"

    def test_sell_beats_buy_returns_sell(self):
        engine = StrategyVotingEngine(consensus_threshold=0.40, min_quorum=1)
        decisions = [
            _decision("sell", 0.7, "s1"),
            _decision("buy", 0.3, "s2"),
        ]
        result = engine.vote(decisions, regime=DEFAULT_REGIME, scope=DEFAULT_SCOPE)
        assert result is not None
        assert result.action == "sell"


# ── 元数据字段 ───────────────────────────────────────────────────────────────

class TestMetadata:
    def setup_method(self):
        self.engine = StrategyVotingEngine(min_quorum=1, consensus_threshold=0.40)
        self.decisions = [
            _decision("buy", 0.7, "s1", used_indicators=["sma20", "ema50"]),
            _decision("buy", 0.6, "s2", used_indicators=["rsi14", "sma20"]),
        ]
        self.result = self.engine.vote(
            self.decisions, regime=RegimeType.RANGING, scope="intrabar"
        )

    def test_regime_in_metadata(self):
        assert self.result is not None
        assert self.result.metadata["regime"] == "ranging"

    def test_scope_in_metadata(self):
        assert self.result is not None
        assert self.result.metadata["scope"] == "intrabar"

    def test_quorum_in_metadata(self):
        assert self.result is not None
        assert self.result.metadata["quorum"] == 2

    def test_participating_strategies(self):
        assert self.result is not None
        assert sorted(self.result.metadata["participating_strategies"]) == ["s1", "s2"]

    def test_used_indicators_deduplicated(self):
        assert self.result is not None
        # sma20 出现两次，but 应去重
        assert self.result.used_indicators.count("sma20") == 1
        assert set(self.result.used_indicators) == {"sma20", "ema50", "rsi14"}

    def test_vote_weights_in_metadata(self):
        assert self.result is not None
        assert "vote_buy_weight" in self.result.metadata
        assert "vote_sell_weight" in self.result.metadata
        assert "vote_hold_weight" in self.result.metadata

    def test_symbol_and_timeframe_from_first_decision(self):
        assert self.result is not None
        assert self.result.symbol == "XAUUSD"
        assert self.result.timeframe == "M1"


# ── describe ─────────────────────────────────────────────────────────────────

class TestDescribe:
    def test_describe_returns_config(self):
        engine = StrategyVotingEngine(
            consensus_threshold=0.45, min_quorum=3, disagreement_penalty=0.30
        )
        desc = engine.describe()
        assert desc["consensus_threshold"] == 0.45
        assert desc["min_quorum"] == 3
        assert desc["disagreement_penalty"] == 0.30


# ── 综合场景 ─────────────────────────────────────────────────────────────────

class TestIntegrationScenarios:
    """模拟真实 8 策略场景。"""

    def _make_engine(self) -> StrategyVotingEngine:
        return StrategyVotingEngine(
            consensus_threshold=0.40, min_quorum=2, disagreement_penalty=0.50
        )

    def test_strong_buy_consensus(self):
        """7 buy + 1 hold → 强烈买入共识。"""
        engine = self._make_engine()
        decisions = [_decision("buy", 0.70, f"s{i}") for i in range(7)]
        decisions.append(_decision("hold", 0.30, "s7"))
        result = engine.vote(decisions, regime=RegimeType.TRENDING, scope="confirmed")
        assert result is not None
        assert result.action == "buy"
        # buy_score ≈ 4.9/5.2 ≈ 0.94
        assert result.confidence > 0.8

    def test_no_consensus_mixed(self):
        """4 buy + 4 sell → 完全对立，无共识。"""
        engine = self._make_engine()
        decisions = (
            [_decision("buy", 0.65, f"b{i}") for i in range(4)]
            + [_decision("sell", 0.65, f"s{i}") for i in range(4)]
        )
        result = engine.vote(decisions, regime=RegimeType.UNCERTAIN, scope="confirmed")
        assert result is None

    def test_sell_wins_over_majority_hold(self):
        """3 sell + 6 hold → sell_score = 3*0.65 / (3*0.65 + 6*0.40) ≈ 0.449 >= 0.40"""
        engine = self._make_engine()
        decisions = (
            [_decision("sell", 0.65, f"s{i}") for i in range(3)]
            + [_decision("hold", 0.40, f"h{i}") for i in range(6)]
        )
        result = engine.vote(decisions, regime=RegimeType.RANGING, scope="confirmed")
        assert result is not None
        assert result.action == "sell"
