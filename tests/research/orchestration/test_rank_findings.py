"""MiningRunner._rank_findings 行为测试。

守护 Gap 2b follow-up：barrier_predictive_power 的 significant 条目必须被
纳入 Top Findings 排序，否则挖掘输出展示层漏掉 barrier 语义信号。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.research.core.config import ResearchConfig
from src.research.core.contracts import (
    DataSummary,
    IndicatorBarrierPredictiveResult,
    IndicatorPredictiveResult,
    MiningResult,
)
from src.research.core.ports import ResearchDataDeps
from src.research.orchestration.runner import MiningRunner
from src.utils.timezone import utc_now


def _runner() -> MiningRunner:
    """构造最小 MiningRunner（_rank_findings 不读 self.deps，MagicMock 够用）。"""
    deps = ResearchDataDeps(
        bar_loader=MagicMock(),
        indicator_computer=MagicMock(),
        regime_detector=MagicMock(),
    )
    return MiningRunner(config=ResearchConfig(), deps=deps)


def _empty_result() -> MiningResult:
    return MiningResult(
        run_id="test",
        started_at=utc_now(),
        data_summary=DataSummary(
            symbol="XAUUSD",
            timeframe="H1",
            n_bars=1000,
            start_time=utc_now(),
            end_time=utc_now(),
            train_bars=700,
            test_bars=300,
            regime_distribution={},
            available_indicators=[],
        ),
    )


def _barrier_result(
    ic: float,
    n: int = 500,
    tp_rate: float = 0.4,
    sl_rate: float = 0.55,
    direction: str = "long",
    is_significant: bool = True,
) -> IndicatorBarrierPredictiveResult:
    return IndicatorBarrierPredictiveResult(
        indicator_name="adx14",
        field_name="adx",
        regime=None,
        direction=direction,
        barrier_key=(2.5, 5.0, 120),
        n_samples=n,
        pearson_r=ic,
        spearman_rho=ic,
        information_coefficient=ic,
        p_value=0.001,
        permutation_p_value=0.001,
        is_significant=is_significant,
        tp_hit_rate=tp_rate,
        sl_hit_rate=sl_rate,
        time_exit_rate=1.0 - tp_rate - sl_rate,
        mean_bars_held=15.5,
        mean_return_pct=-0.005,
    )


class TestBarrierInRankFindings:
    def test_significant_barrier_entry_appears_in_findings(self) -> None:
        runner = _runner()
        result = _empty_result()
        result.barrier_predictive_power = [_barrier_result(ic=-0.229)]

        findings = runner._rank_findings(result)

        assert len(findings) >= 1
        barrier_findings = [f for f in findings if f.category == "barrier"]
        assert len(barrier_findings) == 1
        assert "adx14.adx" in barrier_findings[0].summary
        assert "long" in barrier_findings[0].summary
        assert "IC=-0.229" in barrier_findings[0].summary

    def test_insignificant_barrier_skipped(self) -> None:
        runner = _runner()
        result = _empty_result()
        result.barrier_predictive_power = [
            _barrier_result(ic=-0.1, is_significant=False)
        ]
        findings = runner._rank_findings(result)
        assert all(f.category != "barrier" for f in findings)

    def test_negative_ic_generates_warning_action(self) -> None:
        """IC < 0 → action 应警示'亏钱/回避'。"""
        runner = _runner()
        result = _empty_result()
        result.barrier_predictive_power = [_barrier_result(ic=-0.25, sl_rate=0.71)]
        findings = runner._rank_findings(result)
        barrier = [f for f in findings if f.category == "barrier"][0]
        assert "亏钱" in barrier.action
        assert "71" in barrier.action

    def test_positive_ic_generates_entry_action(self) -> None:
        runner = _runner()
        result = _empty_result()
        result.barrier_predictive_power = [
            _barrier_result(ic=0.18, tp_rate=0.6, sl_rate=0.3)
        ]
        findings = runner._rank_findings(result)
        barrier = [f for f in findings if f.category == "barrier"][0]
        assert "有效" in barrier.action

    def test_score_reflects_ic_magnitude_and_exit_skew(self) -> None:
        """打分公式：|IC| × (1 + max(|tp-0.5|, |sl-0.5|) × 10)。"""
        runner = _runner()
        result = _empty_result()
        # 强 IC + 高 sl_rate → 高分
        result.barrier_predictive_power = [
            _barrier_result(ic=-0.25, tp_rate=0.19, sl_rate=0.81),
            _barrier_result(ic=-0.08, tp_rate=0.48, sl_rate=0.52),
        ]
        findings = runner._rank_findings(result)
        # 强 IC / 高 skew 的排第一
        barrier_sorted = sorted(
            [f for f in findings if f.category == "barrier"],
            key=lambda f: f.significance_score,
            reverse=True,
        )
        assert "IC=-0.250" in barrier_sorted[0].summary

    def test_barrier_coexists_with_predictive_power(self) -> None:
        """barrier 和 predictive_power 条目同时存在不冲突。"""
        runner = _runner()
        result = _empty_result()
        result.predictive_power = [
            IndicatorPredictiveResult(
                indicator_name="rsi14",
                field_name="rsi",
                forward_bars=10,
                regime=None,
                n_samples=200,
                pearson_r=0.15,
                spearman_rho=0.18,
                p_value=0.001,
                hit_rate_above_median=0.55,
                hit_rate_below_median=0.45,
                information_coefficient=0.18,
                is_significant=True,
                permutation_p_value=0.002,
            )
        ]
        result.barrier_predictive_power = [_barrier_result(ic=-0.20)]
        findings = runner._rank_findings(result)
        categories = {f.category for f in findings}
        assert "predictive_power" in categories
        assert "barrier" in categories
