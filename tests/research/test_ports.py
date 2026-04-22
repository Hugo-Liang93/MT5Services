"""Research 数据端口契约测试。

守护两件事：
  1. Fake implementation 能通过 isinstance(runtime_checkable) 与 ResearchDataDeps 构造
  2. 现有生产实现（CachingDataLoader / OptimizedPipeline / MarketRegimeDetector）
     按 structural subtyping 天然符合 Port —— 否则重构破坏现有调用链路
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

from src.clients.mt5_market import OHLC
from src.research.core.ports import (
    BarLoaderPort,
    IndicatorComputerPort,
    RegimeDetectorPort,
    ResearchDataDeps,
)
from src.signals.evaluation.regime import RegimeType, SoftRegimeResult


# ---------------------------------------------------------------------------
# Fake implementations — 确认 Protocol 签名不包含过度约束
# ---------------------------------------------------------------------------


class _FakeBarLoader:
    def preload_warmup_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        warmup_bars: int = 200,
    ) -> List[OHLC]:
        return []

    def load_all_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        return []

    def load_child_bars(
        self,
        symbol: str,
        child_tf: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        return []


class _FakeIndicatorComputer:
    def compute(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        indicators: Optional[List[str]] = None,
        scope: str = "confirmed",
    ) -> Dict[str, Any]:
        return {}


class _FakeRegimeDetector:
    def detect(self, indicators: Dict[str, Dict[str, Any]]) -> RegimeType:
        return RegimeType.UNCERTAIN

    def detect_soft(self, indicators: Dict[str, Dict[str, Any]]) -> SoftRegimeResult:
        return SoftRegimeResult(
            dominant_regime=RegimeType.UNCERTAIN,
            probabilities={
                RegimeType.TRENDING: 0.0,
                RegimeType.RANGING: 0.0,
                RegimeType.BREAKOUT: 0.0,
                RegimeType.UNCERTAIN: 1.0,
            },
        )


class TestPortsStructuralSubtyping:
    """Fake implementation 契约验证。"""

    def test_fake_bar_loader_satisfies_protocol(self) -> None:
        loader: BarLoaderPort = _FakeBarLoader()
        assert isinstance(loader, BarLoaderPort)

    def test_fake_indicator_computer_satisfies_protocol(self) -> None:
        computer: IndicatorComputerPort = _FakeIndicatorComputer()
        assert isinstance(computer, IndicatorComputerPort)

    def test_fake_regime_detector_satisfies_protocol(self) -> None:
        detector: RegimeDetectorPort = _FakeRegimeDetector()
        assert isinstance(detector, RegimeDetectorPort)

    def test_fake_deps_constructs_frozen(self) -> None:
        deps = ResearchDataDeps(
            bar_loader=_FakeBarLoader(),
            indicator_computer=_FakeIndicatorComputer(),
            regime_detector=_FakeRegimeDetector(),
        )
        # frozen=True 保证运行时不可变
        with pytest.raises(AttributeError):
            deps.bar_loader = _FakeBarLoader()  # type: ignore[misc]

    def test_missing_method_fails_protocol_check(self) -> None:
        """缺方法的对象不应通过 isinstance（runtime_checkable 的行为保证）。"""

        class _MissingLoadAll:
            def preload_warmup_bars(
                self,
                symbol: str,
                timeframe: str,
                start_time: datetime,
                warmup_bars: int = 200,
            ) -> List[OHLC]:
                return []

        obj = _MissingLoadAll()
        assert not isinstance(obj, BarLoaderPort)


class TestProductionImplementationsSatisfyPorts:
    """现有生产实现必须天然符合 Port —— 守护重构不破坏 structural subtyping。

    这些测试在不实例化完整依赖（DB、MT5）的前提下仅做类型层验证。
    """

    def test_historical_data_loader_satisfies_bar_loader_port(self) -> None:
        from src.backtesting.data.loader import HistoricalDataLoader

        # 类级别 method 存在性检查（不实例化，避免 DB 依赖）
        assert hasattr(HistoricalDataLoader, "preload_warmup_bars")
        assert hasattr(HistoricalDataLoader, "load_all_bars")

    def test_caching_data_loader_satisfies_bar_loader_port(self) -> None:
        from src.backtesting.data.loader import CachingDataLoader

        assert hasattr(CachingDataLoader, "preload_warmup_bars")
        assert hasattr(CachingDataLoader, "load_all_bars")

    def test_optimized_pipeline_satisfies_indicator_computer_port(self) -> None:
        from src.indicators.engine.pipeline import OptimizedPipeline

        assert hasattr(OptimizedPipeline, "compute")

    def test_market_regime_detector_satisfies_regime_detector_port(self) -> None:
        from src.signals.evaluation.regime import MarketRegimeDetector

        assert hasattr(MarketRegimeDetector, "detect")
        assert hasattr(MarketRegimeDetector, "detect_soft")

    def test_market_regime_detector_instance_satisfies_port(self) -> None:
        """MarketRegimeDetector 无构造依赖，可实际做 isinstance 检查。"""
        from src.signals.evaluation.regime import MarketRegimeDetector

        detector = MarketRegimeDetector()
        assert isinstance(detector, RegimeDetectorPort)


class TestPortDocumentedInvariants:
    """Port 约束在契约中的文档化测试。"""

    def test_deps_holds_three_ports(self) -> None:
        deps = ResearchDataDeps(
            bar_loader=_FakeBarLoader(),
            indicator_computer=_FakeIndicatorComputer(),
            regime_detector=_FakeRegimeDetector(),
        )
        assert deps.bar_loader is not None
        assert deps.indicator_computer is not None
        assert deps.regime_detector is not None

    def test_detect_returns_regime_type(self) -> None:
        detector = _FakeRegimeDetector()
        result = detector.detect({})
        assert isinstance(result, RegimeType)

    def test_detect_soft_returns_soft_regime_result(self) -> None:
        detector = _FakeRegimeDetector()
        result = detector.detect_soft({})
        assert isinstance(result, SoftRegimeResult)
        assert sum(result.probabilities.values()) == pytest.approx(1.0)
