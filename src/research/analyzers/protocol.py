"""Analyzer Protocol — 挖掘分析器统一抽象。

设计目的（2026-04-22 P2 架构修复）：
  对称于 `features/protocol.py` 的 FeatureProvider Protocol。
  消除 `orchestration/runner.py` 中"`if "predictive_power" in analyses`"
  这类字符串硬编码的 if 链，统一通过注册表派发。

每个具体 Analyzer 提供 3 项元数据 + 1 个 analyze 方法：
  - `name`         — 全局唯一标识（与 ResearchConfig 字段名 / MiningResult typed 字段对应）
  - `result_field` — 写入 `MiningResult` 的字段名（typed 字段仍是事实来源）
  - `requires_provider_groups` — 是否需要 FDR provider 分组
  - `analyze(matrix, *, config, provider_groups=None, indicator_filter=None) → Any`

加新 analyzer 流程（未来）：
  1. 实现 Analyzer Protocol（包装 analyze_xxx 纯函数即可）
  2. 在 `analyzers/__init__.py` 调 `register_default_analyzer`
  3. （可选）在 `MiningResult` 加对应 typed 字段

不改 `orchestration/runner.py`，不改 `mining_runner.py` CLI。
"""

from __future__ import annotations

# 延迟导入避免循环
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from typing_extensions import Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.research.core.config import ResearchConfig
    from src.research.core.data_matrix import DataMatrix


@runtime_checkable
class Analyzer(Protocol):
    """挖掘分析器协议接口。

    所有实现此 Protocol 的类需提供：
      - name / result_field / requires_provider_groups 描述性属性
      - analyze(matrix, *, config, ...) 执行分析

    各 analyzer 自由决定返回类型（List[IndicatorPredictiveResult] /
    List[ThresholdSweepResult] / List[MinedRule] 等），由调用方写入对应
    `MiningResult.{result_field}` 字段。
    """

    @property
    def name(self) -> str:
        """Analyzer 全局唯一标识（如 "predictive_power"）。"""
        ...

    @property
    def result_field(self) -> str:
        """MiningResult 上承接结果的字段名（如 "predictive_power"）。"""
        ...

    @property
    def requires_provider_groups(self) -> bool:
        """是否需要 FDR 用的 provider 分组（True → runner 会准备并传入）。"""
        ...

    def analyze(
        self,
        matrix: "DataMatrix",
        *,
        config: "ResearchConfig",
        provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
        indicator_filter: Optional[List[str]] = None,
    ) -> Any:
        """执行分析。返回类型由具体 analyzer 决定。"""
        ...


# ---------------------------------------------------------------------------
# 注册表（模块级单例）
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, Analyzer] = {}


def register_analyzer(analyzer: Analyzer) -> None:
    """注册一个 analyzer。重名直接覆盖（便于测试 monkey-patch）。"""
    if not isinstance(analyzer, Analyzer):
        raise TypeError(f"{analyzer!r} does not satisfy Analyzer Protocol")
    _REGISTRY[analyzer.name] = analyzer


def get_analyzer(name: str) -> Analyzer:
    """按名查 analyzer。未注册抛 KeyError。"""
    if name not in _REGISTRY:
        raise KeyError(
            f"Analyzer '{name}' not registered. "
            f"Available: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def all_analyzer_names() -> List[str]:
    """返回所有已注册 analyzer 名称（保持注册顺序）。"""
    return list(_REGISTRY.keys())


def clear_registry() -> None:
    """测试用：清空注册表。"""
    _REGISTRY.clear()
