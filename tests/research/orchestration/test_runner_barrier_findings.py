"""MiningRunner._rank_findings barrier 过滤测试。

2026-04-27 评估：原 _rank_findings 仅按 is_significant 过滤 barrier，把负
mean_return（成本后亏钱）的显著发现也推入候选；ranking 之外亦无第二道过滤。
按 plan-md-radiant-sparrow.md 决策 1，过滤口径**只用 mean_return_pct > 0**：
经济可行性已由 cost-after mean_return 表达；R:R 偏向 TP 时 tp<sl 仍可正期望，
不应被 hit-rate ratio 门控误杀。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.research.core.contracts import (
    DataSummary,
    IndicatorBarrierPredictiveResult,
    MiningResult,
)
from src.research.orchestration.runner import MiningRunner


def _bp(
    *,
    indicator: str,
    ic: float,
    mean_return_pct: float,
    tp_hit_rate: float = 0.60,
    sl_hit_rate: float = 0.25,
    is_significant: bool = True,
    n_samples: int = 120,
) -> IndicatorBarrierPredictiveResult:
    return IndicatorBarrierPredictiveResult(
        indicator_name=indicator,
        field_name="value",
        regime=None,
        direction="long",
        barrier_key=(1.5, 2.0, 30),
        n_samples=n_samples,
        pearson_r=ic,
        spearman_rho=ic,
        information_coefficient=ic,
        p_value=0.01,
        permutation_p_value=0.01,
        is_significant=is_significant,
        tp_hit_rate=tp_hit_rate,
        sl_hit_rate=sl_hit_rate,
        time_exit_rate=max(0.0, 1.0 - tp_hit_rate - sl_hit_rate),
        mean_bars_held=12.0,
        mean_return_pct=mean_return_pct,
    )


def _result(items: list[IndicatorBarrierPredictiveResult]) -> MiningResult:
    return MiningResult(
        run_id="test",
        started_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        data_summary=DataSummary(
            symbol="XAUUSD",
            timeframe="H1",
            n_bars=500,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 4, 1, tzinfo=timezone.utc),
            train_bars=350,
            test_bars=150,
            regime_distribution={},
            available_indicators=[],
        ),
        barrier_predictive_power=items,
    )


def test_barrier_top_findings_exclude_negative_mean_return() -> None:
    """显著但成本后负收益的 barrier finding 必须被剔除。"""
    runner = object.__new__(MiningRunner)

    findings = runner._rank_findings(
        _result(
            [
                _bp(indicator="bad", ic=0.35, mean_return_pct=-0.04),
                _bp(indicator="good", ic=0.20, mean_return_pct=0.03),
            ]
        )
    )

    summaries = [f.summary for f in findings]
    assert any("good.value" in s for s in summaries)
    assert not any("bad.value" in s for s in summaries)


def test_barrier_top_findings_exclude_insignificant() -> None:
    """非显著（is_significant=False）即使 mean_return 正也不入榜。"""
    runner = object.__new__(MiningRunner)

    findings = runner._rank_findings(
        _result(
            [
                _bp(
                    indicator="not_sig",
                    ic=0.10,
                    mean_return_pct=0.02,
                    is_significant=False,
                ),
                _bp(indicator="sig", ic=0.20, mean_return_pct=0.02),
            ]
        )
    )

    summaries = [f.summary for f in findings]
    assert any("sig.value" in s for s in summaries)
    assert not any("not_sig.value" in s for s in summaries)


def test_barrier_top_findings_keep_high_rr_low_winrate() -> None:
    """高赔率低胜率组合（tp_hit < sl_hit 但 mean_return > 0）必须保留。

    决策 1：只用 mean_return 表达经济可行性。R:R 偏向 TP 时即便胜率
    低于止损率，平均收益仍可为正——这类 trend-following 风格不该被
    hit-rate ratio 门控误杀。
    """
    runner = object.__new__(MiningRunner)

    findings = runner._rank_findings(
        _result(
            [
                _bp(
                    indicator="high_rr",
                    ic=0.25,
                    mean_return_pct=0.015,
                    tp_hit_rate=0.30,
                    sl_hit_rate=0.45,
                ),
            ]
        )
    )

    summaries = [f.summary for f in findings]
    assert any(
        "high_rr.value" in s for s in summaries
    ), "tp<sl 但 mean_return>0 的高赔率组合应保留——决策 1"


def test_barrier_summary_includes_mean_return() -> None:
    """barrier finding summary 必须暴露 mean_return_pct，可审计。"""
    runner = object.__new__(MiningRunner)

    findings = runner._rank_findings(
        _result([_bp(indicator="audit", ic=0.20, mean_return_pct=0.025)])
    )

    summary = next(f.summary for f in findings if "audit.value" in f.summary)
    assert "ret=" in summary
    assert "+0.0250" in summary or "+0.025" in summary
