"""src/research/features/protocol.py

FeatureProvider Protocol 及相关类型定义。

此模块定义：
- FeatureRole        — 特征在策略信号结构中的角色枚举
- ProviderDataRequirement — Provider 声明其所需的额外数据（父 TF 指标等）
- FeatureComputeResult   — 可变结果聚合容器，用于汇总多个 Provider 的计算摘要
- FeatureProvider        — runtime_checkable Protocol，所有特征计算器须满足此接口
- PROMOTED_INDICATOR_PRECEDENTS — 已从研究特征晋升为生产指标的历史记录
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from typing_extensions import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# FeatureRole
# ---------------------------------------------------------------------------


class FeatureRole(str, Enum):
    """特征在策略信号三层结构（Why/When/Where）中的角色。"""

    WHY = "why"
    WHEN = "when"
    WHERE = "where"
    VOLUME = "volume"


# ---------------------------------------------------------------------------
# ProviderDataRequirement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDataRequirement:
    """Provider 声明其额外数据需求（父 TF 的指标值）。

    用于在 FeatureProvider.compute() 调用前，由调度层预先准备好
    跨 TF 数据并注入 extra_data 字典。

    Attributes:
        parent_tf_mapping: 本 TF → 父 TF 的映射，如 {"M30": "H4"}
        parent_indicators: 需要从父 TF 取出的指标名列表，如 ["supertrend_direction"]
    """

    parent_tf_mapping: Dict[str, str]
    parent_indicators: List[str]


# ---------------------------------------------------------------------------
# FeatureComputeResult
# ---------------------------------------------------------------------------


class FeatureComputeResult:
    """多个 Provider 计算结果的聚合摘要（可变容器）。

    不包含实际特征值——特征值直接合并到 DataMatrix.indicator_series。
    此对象仅用于运行时日志与监控。
    """

    def __init__(self) -> None:
        self.provider_summaries: Dict[str, Dict[str, Any]] = {}

    @property
    def total_features(self) -> int:
        """所有 Provider 产出的特征总数。"""
        return sum(
            s.get("feature_count", 0) for s in self.provider_summaries.values()
        )

    def add(
        self,
        provider_name: str,
        feature_count: int,
        elapsed_sec: float,
    ) -> None:
        """记录单个 Provider 的计算结果摘要。

        Args:
            provider_name: Provider 的唯一名称（对应 FeatureProvider.name）
            feature_count: 该 Provider 本次产出的特征列数
            elapsed_sec:   该 Provider 计算耗时（秒）
        """
        self.provider_summaries[provider_name] = {
            "feature_count": feature_count,
            "elapsed_sec": elapsed_sec,
        }

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可日志/监控的字典格式。"""
        return {
            "total_features": self.total_features,
            "providers": dict(self.provider_summaries),
        }


# ---------------------------------------------------------------------------
# FeatureProvider Protocol
# ---------------------------------------------------------------------------

# 延迟导入以避免循环依赖；DataMatrix 只在 compute() 签名里使用
if TYPE_CHECKING:
    from src.research.core.data_matrix import DataMatrix


@runtime_checkable
class FeatureProvider(Protocol):
    """特征计算器协议接口。

    所有实现此 Protocol 的类需提供：
    - name / feature_count 描述性属性
    - required_columns()  声明依赖的原始指标列
    - required_extra_data() 声明跨 TF 数据需求（可无）
    - role_mapping()      声明每个输出特征的策略角色
    - compute()           执行特征计算，返回新特征列

    compute() 的返回值键格式与 DataMatrix.indicator_series 一致：
        {(indicator_name, field_name): [Optional[float], ...]}
    """

    @property
    def name(self) -> str:
        """Provider 的全局唯一标识名称。"""
        ...

    @property
    def feature_count(self) -> int:
        """该 Provider 固定产出的特征列数（用于预分配与监控）。"""
        ...

    def required_columns(self) -> List[Tuple[str, str]]:
        """声明 compute() 所需的 DataMatrix.indicator_series 键列表。

        Returns:
            [(indicator_name, field_name), ...]
        """
        ...

    def required_extra_data(self) -> Optional[ProviderDataRequirement]:
        """声明跨 TF 数据需求；无需额外数据时返回 None。"""
        ...

    def role_mapping(self) -> Dict[str, FeatureRole]:
        """声明每个输出特征字段名对应的策略角色。

        Returns:
            {feature_field_name: FeatureRole}
        """
        ...

    def compute(
        self,
        matrix: Any,  # DataMatrix，避免循环导入使用 Any
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[Tuple[str, str], List[Optional[float]]]:
        """执行特征计算。

        Args:
            matrix:     DataMatrix 实例（含对齐的 OHLC + indicator_series）
            extra_data: 由调度层根据 required_extra_data() 准备的跨 TF 数据

        Returns:
            新特征列，格式与 DataMatrix.indicator_series 一致：
            {(indicator_name, field_name): [Optional[float] × n_bars]}
        """
        ...


# ---------------------------------------------------------------------------
# PROMOTED_INDICATOR_PRECEDENTS
# ---------------------------------------------------------------------------

PROMOTED_INDICATOR_PRECEDENTS: Tuple[Dict[str, Any], ...] = (
    {
        "feature_name": "di_spread",
        "promoted_indicator_name": "di_spread14",
        "status": "promoted_indicator",
        "note": "已晋升为共享趋势方向复合指标",
    },
    {
        "feature_name": "squeeze_score",
        "promoted_indicator_name": "squeeze20",
        "status": "promoted_indicator",
        "note": "已晋升为共享波动率挤压指标",
    },
    {
        "feature_name": "vwap_gap_atr",
        "promoted_indicator_name": "vwap_dev30",
        "status": "promoted_indicator",
        "note": "已晋升为共享均值偏离指标",
    },
    {
        "feature_name": "rsi_accel",
        "promoted_indicator_name": "momentum_accel14",
        "status": "promoted_indicator",
        "note": "已晋升为共享动量加速度指标",
    },
    {
        "feature_name": "momentum_consensus",
        "promoted_indicator_name": "momentum_consensus14",
        "status": "promoted_indicator",
        "note": "首轮 feature promotion 交付的新共享动量一致性指标",
    },
)
