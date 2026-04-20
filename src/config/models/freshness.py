"""Freshness 阈值 Pydantic 配置模型（P9 Phase 3.2）。

为 ``/v1/execution/workbench`` 顶层 ``freshness_hints`` 字段提供集中管理的默认值。
前端可读取这些 hint 决定本地"fresh / warn / stale"三态阈值；后端可根据 ini
覆盖（未来扩展点）。

阈值来源：``docs/design/quantx-data-freshness-tiering.md`` §6.2。
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, Field


class _FreshnessThreshold(BaseModel):
    """单块的 freshness 阈值 + tier 标签。"""

    stale_after_seconds: int = Field(ge=1, description="age < 此值视为 fresh（绿）")
    stale_threshold_seconds: int = Field(
        ge=1, description="age >= 此值视为 stale（红，T0/T1 应禁用交易按钮）"
    )
    tier: str = Field(
        pattern=r"^(T0|T1|T2|T3|T4|static)$",
        description="对齐 docs/design/quantx-data-freshness-tiering.md",
    )


class FreshnessConfig(BaseModel):
    """8 块 freshness 阈值集合。

    默认值与设计文档 §6.2 表一致：
    - T0 (tradability/risk)：stale 阈值最严（10s/30s）
    - T1 (positions/orders/pending)：60s
    - T2 (exposure/quote)：120s/30s
    - T3 (events)：300s（5 分钟）

    实例化后调用 ``as_hints()`` 转为 workbench payload 字典格式。
    """

    tradability: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=2, stale_threshold_seconds=10, tier="T0"
    )
    risk: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=5, stale_threshold_seconds=30, tier="T0"
    )
    positions: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=10, stale_threshold_seconds=60, tier="T1"
    )
    orders: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=10, stale_threshold_seconds=60, tier="T1"
    )
    pending: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=10, stale_threshold_seconds=60, tier="T1"
    )
    exposure: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=15, stale_threshold_seconds=120, tier="T2"
    )
    quote: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=5, stale_threshold_seconds=30, tier="T2"
    )
    events: _FreshnessThreshold = _FreshnessThreshold(
        stale_after_seconds=30, stale_threshold_seconds=300, tier="T3"
    )

    def as_hints(self) -> dict[str, dict[str, Any]]:
        """转换为 ``/v1/execution/workbench`` ``freshness_hints`` 字段格式。

        每个块输出 ``{stale_after_seconds, stale_threshold_seconds, tier}``。
        """
        return {
            block_name: dict(getattr(self, block_name).model_dump())
            for block_name in self.model_fields.keys()
        }


def default_freshness_config() -> FreshnessConfig:
    """返回默认 FreshnessConfig 实例（所有字段用 model 默认值）。"""
    return FreshnessConfig()
