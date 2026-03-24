"""recommendation.py 单元测试。"""

from __future__ import annotations

import configparser
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.backtesting.models import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    ParamChange,
    Recommendation,
    RecommendationStatus,
)
from src.backtesting.recommendation import (
    MAX_CHANGE_PCT,
    MAX_CHANGES_PER_REC,
    ConfigApplicator,
    RecommendationEngine,
)

# ── Test Fixtures ────────────────────────────────────────────────────


def _make_metrics(
    total_trades: int = 50,
    win_rate: float = 55.0,
    sharpe: float = 1.2,
    max_dd: float = -5.0,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=total_trades,
        winning_trades=int(total_trades * win_rate / 100),
        losing_trades=total_trades - int(total_trades * win_rate / 100),
        win_rate=win_rate,
        expectancy=2.5,
        profit_factor=1.8,
        sharpe_ratio=sharpe,
        sortino_ratio=1.5,
        max_drawdown=max_dd,
        max_drawdown_duration=20,
        avg_win=50.0,
        avg_loss=-30.0,
        avg_bars_held=5.0,
        total_pnl=500.0,
        total_pnl_pct=5.0,
        calmar_ratio=1.0,
    )


def _make_result(
    param_set: Optional[Dict[str, Any]] = None,
    metrics: Optional[BacktestMetrics] = None,
    metrics_by_strategy: Optional[Dict[str, BacktestMetrics]] = None,
    metrics_by_regime: Optional[Dict[str, BacktestMetrics]] = None,
) -> BacktestResult:
    now = datetime.now(timezone.utc)
    return BacktestResult(
        config=BacktestConfig(
            symbol="XAUUSD",
            timeframe="M5",
            start_time=now,
            end_time=now,
        ),
        run_id="bt_test",
        started_at=now,
        completed_at=now,
        trades=[],
        equity_curve=[],
        metrics=metrics or _make_metrics(),
        metrics_by_regime=metrics_by_regime or {},
        metrics_by_strategy=metrics_by_strategy or {},
        metrics_by_confidence={},
        param_set=param_set or {},
    )


@dataclass
class MockWalkForwardSplit:
    split_index: int
    train_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    train_end: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    test_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    test_end: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    best_params: Dict[str, Any] = field(default_factory=dict)
    in_sample_result: Any = None
    out_of_sample_result: Any = None


@dataclass
class MockWalkForwardResult:
    splits: List[MockWalkForwardSplit]
    aggregate_metrics: BacktestMetrics
    overfitting_ratio: float
    consistency_rate: float
    config: Any = None


def _make_wf_result(
    n_splits: int = 5,
    overfitting_ratio: float = 1.5,
    consistency_rate: float = 0.8,
    total_trades: int = 50,
    win_rate: float = 55.0,
    sharpe: float = 1.2,
    split_params: Optional[List[Dict[str, Any]]] = None,
    split_metrics_by_strategy: Optional[Dict[str, BacktestMetrics]] = None,
    split_metrics_by_regime: Optional[Dict[str, BacktestMetrics]] = None,
) -> MockWalkForwardResult:
    agg = _make_metrics(total_trades=total_trades, win_rate=win_rate, sharpe=sharpe)
    splits = []
    for i in range(n_splits):
        params = (
            split_params[i]
            if split_params and i < len(split_params)
            else {"rsi_reversion__oversold": 25.0 + i}
        )
        oos_result = _make_result(
            param_set=params,
            metrics_by_strategy=split_metrics_by_strategy or {},
            metrics_by_regime=split_metrics_by_regime or {},
        )
        splits.append(
            MockWalkForwardSplit(
                split_index=i,
                best_params=params,
                in_sample_result=_make_result(param_set=params),
                out_of_sample_result=oos_result,
            )
        )
    return MockWalkForwardResult(
        splits=splits,
        aggregate_metrics=agg,
        overfitting_ratio=overfitting_ratio,
        consistency_rate=consistency_rate,
    )


# ── RecommendationEngine Tests ──────────────────────────────────────


class TestRecommendationEngineSafety:
    """安全门槛检查测试。"""

    def test_rejects_high_overfitting(self) -> None:
        wf = _make_wf_result(overfitting_ratio=2.5)
        engine = RecommendationEngine()
        with pytest.raises(ValueError, match="过拟合比过高"):
            engine.generate(wf, "wf_test")

    def test_rejects_exact_threshold_overfitting(self) -> None:
        wf = _make_wf_result(overfitting_ratio=2.0)
        engine = RecommendationEngine()
        with pytest.raises(ValueError, match="过拟合比过高"):
            engine.generate(wf, "wf_test")

    def test_rejects_low_consistency(self) -> None:
        wf = _make_wf_result(consistency_rate=0.4)
        engine = RecommendationEngine()
        with pytest.raises(ValueError, match="OOS 一致性不足"):
            engine.generate(wf, "wf_test")

    def test_rejects_low_sample(self) -> None:
        wf = _make_wf_result(total_trades=20)
        engine = RecommendationEngine()
        with pytest.raises(ValueError, match="OOS 交易样本不足"):
            engine.generate(wf, "wf_test")

    def test_accepts_valid_result(self) -> None:
        wf = _make_wf_result(
            overfitting_ratio=1.5, consistency_rate=0.8, total_trades=50
        )
        engine = RecommendationEngine()
        rec = engine.generate(wf, "wf_test")
        assert rec.status == RecommendationStatus.PENDING
        assert rec.source_run_id == "wf_test"


class TestParamAggregation:
    """参数聚合逻辑测试。"""

    def test_uses_median(self) -> None:
        """多窗口 best_params 取中位数。"""
        split_params = [
            {"rsi_reversion__oversold": 20.0},
            {"rsi_reversion__oversold": 25.0},
            {"rsi_reversion__oversold": 30.0},
            {"rsi_reversion__oversold": 28.0},
            {"rsi_reversion__oversold": 22.0},
        ]
        wf = _make_wf_result(split_params=split_params)
        engine = RecommendationEngine()
        rec = engine.generate(
            wf,
            "wf_test",
            current_strategy_params={"rsi_reversion__oversold": 30.0},
        )
        # 中位数 = 25.0
        strategy_changes = [c for c in rec.changes if c.section == "strategy_params"]
        assert len(strategy_changes) >= 1
        rsi_change = next(
            c for c in strategy_changes if c.key == "rsi_reversion__oversold"
        )
        assert rsi_change.new_value == 25.0

    def test_skips_single_window_param(self) -> None:
        """只在 1 个窗口出现的参数不推荐（样本不足）。"""
        split_params = [
            {"rsi_reversion__oversold": 25.0, "rare__param": 1.0},
            {"rsi_reversion__oversold": 26.0},
            {"rsi_reversion__oversold": 27.0},
        ]
        wf = _make_wf_result(n_splits=3, split_params=split_params)
        engine = RecommendationEngine()
        rec = engine.generate(
            wf,
            "wf_test",
            current_strategy_params={"rsi_reversion__oversold": 30.0},
        )
        keys = [c.key for c in rec.changes]
        assert "rare__param" not in keys

    def test_skips_tiny_changes(self) -> None:
        """变更 < 1% 的参数不推荐。"""
        split_params = [
            {"rsi_reversion__oversold": 30.0},
            {"rsi_reversion__oversold": 30.1},
            {"rsi_reversion__oversold": 30.0},
        ]
        wf = _make_wf_result(n_splits=3, split_params=split_params)
        engine = RecommendationEngine()
        rec = engine.generate(
            wf,
            "wf_test",
            current_strategy_params={"rsi_reversion__oversold": 30.0},
        )
        strategy_changes = [c for c in rec.changes if c.section == "strategy_params"]
        # 中位数 30.0 vs 当前 30.0 → 变更 0% → 跳过
        assert len(strategy_changes) == 0


class TestChangeClipping:
    """变更幅度裁剪测试。"""

    def test_clips_large_change(self) -> None:
        """超过 30% 变更被裁剪到 30%。"""
        # 当前值 20，推荐值 40 → 100% 变更 → 裁剪到 +30% = 26
        split_params = [
            {"rsi_reversion__oversold": 40.0},
            {"rsi_reversion__oversold": 40.0},
            {"rsi_reversion__oversold": 40.0},
        ]
        wf = _make_wf_result(n_splits=3, split_params=split_params)
        engine = RecommendationEngine()
        rec = engine.generate(
            wf,
            "wf_test",
            current_strategy_params={"rsi_reversion__oversold": 20.0},
        )
        strategy_changes = [c for c in rec.changes if c.section == "strategy_params"]
        assert len(strategy_changes) == 1
        c = strategy_changes[0]
        assert abs(c.change_pct) <= MAX_CHANGE_PCT + 0.01
        assert c.new_value == pytest.approx(26.0, abs=0.01)

    def test_clips_negative_change(self) -> None:
        """负方向裁剪。"""
        split_params = [
            {"rsi_reversion__oversold": 10.0},
            {"rsi_reversion__oversold": 10.0},
            {"rsi_reversion__oversold": 10.0},
        ]
        wf = _make_wf_result(n_splits=3, split_params=split_params)
        engine = RecommendationEngine()
        rec = engine.generate(
            wf,
            "wf_test",
            current_strategy_params={"rsi_reversion__oversold": 30.0},
        )
        strategy_changes = [c for c in rec.changes if c.section == "strategy_params"]
        assert len(strategy_changes) == 1
        c = strategy_changes[0]
        assert abs(c.change_pct) <= MAX_CHANGE_PCT + 0.01
        # 30 * (1 - 0.30) = 21.0
        assert c.new_value == pytest.approx(21.0, abs=0.01)


class TestMaxChangesLimit:
    """最大变更数量限制测试。"""

    def test_limits_to_max(self) -> None:
        """超过 10 个变更被裁剪。"""
        params = {f"strat{i}__param": float(10 + i * 5) for i in range(15)}
        split_params = [params] * 3
        current = {f"strat{i}__param": 10.0 for i in range(15)}
        wf = _make_wf_result(n_splits=3, split_params=split_params)
        engine = RecommendationEngine()
        rec = engine.generate(wf, "wf_test", current_strategy_params=current)
        assert len(rec.changes) <= MAX_CHANGES_PER_REC


class TestStatusTransitions:
    """推荐状态转换测试。"""

    def test_initial_status_pending(self) -> None:
        wf = _make_wf_result()
        engine = RecommendationEngine()
        rec = engine.generate(wf, "wf_test")
        assert rec.status == RecommendationStatus.PENDING

    def test_approve_sets_status(self) -> None:
        wf = _make_wf_result()
        engine = RecommendationEngine()
        rec = engine.generate(wf, "wf_test")
        rec.status = RecommendationStatus.APPROVED
        rec.approved_at = datetime.now(timezone.utc)
        assert rec.status == RecommendationStatus.APPROVED
        assert rec.approved_at is not None

    def test_to_dict_roundtrip(self) -> None:
        wf = _make_wf_result()
        engine = RecommendationEngine()
        rec = engine.generate(wf, "wf_test")
        d = rec.to_dict()
        assert d["status"] == "pending"
        assert d["rec_id"].startswith("rec_")
        assert isinstance(d["changes"], list)


class TestRationale:
    """推荐理由生成测试。"""

    def test_contains_wf_summary(self) -> None:
        wf = _make_wf_result(n_splits=5, overfitting_ratio=1.3, consistency_rate=0.8)
        engine = RecommendationEngine()
        rec = engine.generate(
            wf,
            "wf_test",
            current_strategy_params={"rsi_reversion__oversold": 30.0},
        )
        assert "Walk-Forward" in rec.rationale
        assert "5 窗口" in rec.rationale
        assert "1.30" in rec.rationale


# ── ConfigApplicator Tests ───────────────────────────────────────────


class TestConfigApplicator:
    """配置应用器测试。"""

    def _make_rec(
        self,
        status: RecommendationStatus = RecommendationStatus.APPROVED,
    ) -> Recommendation:
        return Recommendation(
            rec_id="rec_test123",
            source_run_id="wf_test",
            created_at=datetime.now(timezone.utc),
            status=status,
            overfitting_ratio=1.5,
            consistency_rate=0.8,
            oos_sharpe=1.2,
            oos_win_rate=55.0,
            oos_total_trades=50,
            changes=[
                ParamChange(
                    section="strategy_params",
                    key="rsi_reversion__oversold",
                    old_value=30.0,
                    new_value=26.0,
                    change_pct=-13.3,
                ),
                ParamChange(
                    section="regime_affinity.supertrend",
                    key="ranging",
                    old_value=0.15,
                    new_value=0.19,
                    change_pct=26.7,
                ),
            ],
            rationale="test rationale",
        )

    def test_apply_requires_approved(self) -> None:
        rec = self._make_rec(status=RecommendationStatus.PENDING)
        applicator = ConfigApplicator()
        with pytest.raises(ValueError, match="需要 approved 状态"):
            applicator.apply(rec)

    def test_apply_writes_local_ini(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            rec = self._make_rec()
            applicator = ConfigApplicator(config_dir=config_dir)
            applicator.apply(rec)

            # 验证 signal.local.ini 已写入
            local_ini = config_dir / "signal.local.ini"
            assert local_ini.exists()

            parser = configparser.ConfigParser()
            parser.read(str(local_ini))

            assert parser.has_section("strategy_params")
            assert parser.get("strategy_params", "rsi_reversion__oversold") == "26.0"
            assert parser.has_section("regime_affinity.supertrend")
            assert parser.get("regime_affinity.supertrend", "ranging") == "0.19"

            # 验证状态更新
            assert rec.status == RecommendationStatus.APPLIED
            assert rec.applied_at is not None
            assert rec.backup_path is not None

    def test_apply_calls_signal_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            mock_module = MagicMock()
            rec = self._make_rec()
            applicator = ConfigApplicator(
                config_dir=config_dir, signal_module=mock_module
            )
            applicator.apply(rec)

            mock_module.apply_param_overrides.assert_called_once()
            call_args = mock_module.apply_param_overrides.call_args
            strategy_params = call_args[0][0]
            affinity_overrides = call_args[0][1]
            assert strategy_params["rsi_reversion__oversold"] == 26.0
            assert affinity_overrides["supertrend"]["ranging"] == 0.19

    def test_apply_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            # 创建一个已有的 signal.local.ini
            existing_ini = config_dir / "signal.local.ini"
            existing_ini.write_text("[strategy_params]\nold_key = 1.0\n")

            rec = self._make_rec()
            applicator = ConfigApplicator(config_dir=config_dir)
            backup_path = applicator.apply(rec)

            assert Path(backup_path).exists()
            backup_content = Path(backup_path).read_text()
            assert "old_key" in backup_content

    def test_rollback_requires_applied(self) -> None:
        rec = self._make_rec(status=RecommendationStatus.APPROVED)
        applicator = ConfigApplicator()
        with pytest.raises(ValueError, match="需要 applied 状态"):
            applicator.rollback(rec)

    def test_rollback_restores_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            # 先 apply
            rec = self._make_rec()
            applicator = ConfigApplicator(config_dir=config_dir)
            applicator.apply(rec)

            # 验证 local.ini 被改了
            local_ini = config_dir / "signal.local.ini"
            parser = configparser.ConfigParser()
            parser.read(str(local_ini))
            assert parser.has_section("strategy_params")

            # 回滚
            applicator.rollback(rec)
            assert rec.status == RecommendationStatus.ROLLED_BACK
            assert rec.rolled_back_at is not None

    def test_apply_preserves_existing_sections(self) -> None:
        """Apply 保留 signal.local.ini 中已有的其他 section。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            existing_ini = config_dir / "signal.local.ini"
            existing_ini.write_text(
                "[signal]\nauto_trade_enabled = false\n"
                "[strategy_params]\nold_param = 42.0\n"
            )

            rec = self._make_rec()
            applicator = ConfigApplicator(config_dir=config_dir)
            applicator.apply(rec)

            parser = configparser.ConfigParser()
            parser.read(str(config_dir / "signal.local.ini"))
            # 新参数写入
            assert parser.get("strategy_params", "rsi_reversion__oversold") == "26.0"
            # 旧参数保留
            assert parser.get("strategy_params", "old_param") == "42.0"
            # 其他 section 保留
            assert parser.get("signal", "auto_trade_enabled") == "false"
