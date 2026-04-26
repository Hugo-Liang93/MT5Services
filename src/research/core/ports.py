"""Research 核心域的数据依赖端口。

研究核心域（core / analyzers / features / strategies / orchestration）通过这组
Protocol 声明所需的底层能力，不直接 import backtesting 或 signals 的实现。

backtesting / signals / indicators 的现有类（CachingDataLoader / OptimizedPipeline /
MarketRegimeDetector）按 structural subtyping 天然符合这些 Protocol，装配层
（CLI / API / nightly runner）负责把实例聚合为 ResearchDataDeps 注入。

约束（源于 CLAUDE.md §12 协作执行纪律）：
  - 方法签名与现有实现严格一致；不得通过 Port 制造"伪契约"绕开重构
  - 调用方不得 Optional[deps] + fallback —— deps 是强制构造参数
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from src.clients.mt5_market import OHLC
from src.signals.evaluation.regime import RegimeType, SoftRegimeResult


@runtime_checkable
class BarLoaderPort(Protocol):
    """按时间窗口加载 OHLC 的能力。"""

    def preload_warmup_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        warmup_bars: int = 200,
    ) -> List[OHLC]:
        ...

    def load_all_bars(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        ...

    def load_child_bars(
        self,
        symbol: str,
        child_tf: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[OHLC]:
        ...


@runtime_checkable
class IndicatorComputerPort(Protocol):
    """在给定 bar 窗口上计算指标快照的能力。

    返回 `Dict[indicator_name, Dict[field, value]]`，与 OptimizedPipeline.compute
    的运行时返回形状一致（静态类型标注为 Dict[str, Any] 以匹配现有实现）。
    """

    def compute(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Any],
        indicators: Optional[List[str]] = None,
        scope: str = "confirmed",
    ) -> Dict[str, Any]:
        ...


@runtime_checkable
class RegimeDetectorPort(Protocol):
    """从指标快照推断市场 Regime 的能力。"""

    def detect(self, indicators: Dict[str, Dict[str, Any]]) -> RegimeType:
        ...

    def detect_soft(self, indicators: Dict[str, Dict[str, Any]]) -> SoftRegimeResult:
        ...


@dataclass(frozen=True)
class ResearchDataDeps:
    """研究核心域所需的一组数据能力。

    装配层（CLI / API / nightly runner）构造后注入 build_data_matrix / MiningRunner。
    frozen=True 避免运行时被改写成可变配置包。

    §0cc P2：``cleanup_fn`` 让装配层把"如何释放底层 writer/pipeline"的所有权
    显式回传给消费方——consumer 调 ``close()`` 或 ``with deps:`` 即可级联清理，
    不再"装配后撒手不管"导致 mining 入口连接池/线程池泄漏。
    """

    bar_loader: BarLoaderPort
    indicator_computer: IndicatorComputerPort
    regime_detector: RegimeDetectorPort
    cleanup_fn: Optional[Callable[[], None]] = None

    def close(self) -> None:
        """释放装配层托管的 writer / pipeline 资源。

        若构造时未传 cleanup_fn（兼容旧调用方）则 no-op；
        失败被吞但记日志，避免 cleanup 链条卡在中间。
        """
        if self.cleanup_fn is None:
            return
        try:
            self.cleanup_fn()
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "ResearchDataDeps.close() failed", exc_info=True
            )

    def __enter__(self) -> "ResearchDataDeps":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = [
    "BarLoaderPort",
    "IndicatorComputerPort",
    "RegimeDetectorPort",
    "ResearchDataDeps",
]
