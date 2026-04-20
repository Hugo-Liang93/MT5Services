"""FreshnessConfig 测试（P9 Phase 3.2）。

验证：
- 默认值与 docs/design/quantx-data-freshness-tiering.md §6.2 一致
- as_hints() 输出 8 块每块 3 字段（stale_after_seconds / stale_threshold_seconds / tier）
- 部分覆盖生效，未覆盖字段保留默认
- 非法 tier 字符串触发 Pydantic 校验错误
- DEFAULT_FRESHNESS_HINTS 与 default_freshness_config().as_hints() 内容一致
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.models import FreshnessConfig, default_freshness_config
from src.readmodels.workbench import DEFAULT_FRESHNESS_HINTS


def test_default_freshness_config_emits_eight_blocks() -> None:
    hints = default_freshness_config().as_hints()
    expected_blocks = {
        "tradability",
        "risk",
        "positions",
        "orders",
        "pending",
        "exposure",
        "quote",
        "events",
    }
    assert set(hints.keys()) == expected_blocks


def test_default_freshness_config_matches_design_doc_thresholds() -> None:
    """与 docs/design/quantx-data-freshness-tiering.md §6.2 表对齐。"""
    hints = default_freshness_config().as_hints()
    assert hints["tradability"] == {
        "stale_after_seconds": 2,
        "stale_threshold_seconds": 10,
        "tier": "T0",
    }
    assert hints["risk"]["tier"] == "T0"
    assert hints["positions"]["tier"] == "T1"
    assert hints["pending"]["tier"] == "T1"
    assert hints["exposure"]["tier"] == "T2"
    assert hints["events"] == {
        "stale_after_seconds": 30,
        "stale_threshold_seconds": 300,
        "tier": "T3",
    }


def test_workbench_module_constant_matches_default_config() -> None:
    """DEFAULT_FRESHNESS_HINTS 必须与 default_freshness_config().as_hints() 一致，
    保证向后兼容（旧代码引用 DEFAULT_FRESHNESS_HINTS 不破坏）。"""
    assert dict(DEFAULT_FRESHNESS_HINTS) == default_freshness_config().as_hints()


def test_freshness_config_partial_override_keeps_other_defaults() -> None:
    """覆盖单个块字段，其他块保留默认值。"""
    config = FreshnessConfig.model_validate(
        {
            "tradability": {
                "stale_after_seconds": 1,
                "stale_threshold_seconds": 5,
                "tier": "T0",
            }
        }
    )
    hints = config.as_hints()
    assert hints["tradability"]["stale_after_seconds"] == 1
    assert hints["tradability"]["stale_threshold_seconds"] == 5
    # 其他块仍是默认
    assert hints["risk"]["stale_after_seconds"] == 5
    assert hints["events"]["stale_threshold_seconds"] == 300


def test_freshness_config_rejects_invalid_tier() -> None:
    with pytest.raises(ValidationError, match="tier"):
        FreshnessConfig.model_validate(
            {
                "tradability": {
                    "stale_after_seconds": 2,
                    "stale_threshold_seconds": 10,
                    "tier": "T99",  # 非法
                }
            }
        )


def test_freshness_config_rejects_zero_or_negative_seconds() -> None:
    with pytest.raises(ValidationError):
        FreshnessConfig.model_validate(
            {
                "tradability": {
                    "stale_after_seconds": 0,  # ge=1
                    "stale_threshold_seconds": 10,
                    "tier": "T0",
                }
            }
        )


def test_workbench_uses_injected_freshness_config() -> None:
    """WorkbenchReadModel 接受自定义 FreshnessConfig 注入。"""
    from types import SimpleNamespace

    from src.readmodels.workbench import WorkbenchReadModel

    class _StubRuntime:
        def tradability_state_summary(self):
            return {"runtime_present": True}

        def trade_executor_summary(self):
            return {}

        def account_risk_state_summary(self):
            return {"updated_at": "..."}

        def position_runtime_state_payload(self, *, statuses=None, limit=20):
            return {"count": 0, "items": [], "status_counts": {}}

        def active_pending_order_payload(self, *, limit=100):
            return {"count": 0, "items": [], "status_counts": {}}

        def pending_order_lifecycle_payload(self, *, limit=100):
            return {"count": 0, "items": []}

        def pending_execution_context_payload(self):
            return {"items": []}

        def recent_trade_pipeline_events_payload(self, *, limit=50):
            return {"count": 0, "items": []}

        def pending_entries_summary(self):
            return {"entries": []}

    custom = FreshnessConfig.model_validate(
        {
            "tradability": {
                "stale_after_seconds": 1,
                "stale_threshold_seconds": 3,
                "tier": "T0",
            }
        }
    )
    workbench = WorkbenchReadModel(
        runtime_read_model=_StubRuntime(),  # type: ignore[arg-type]
        runtime_identity=SimpleNamespace(account_alias="live-main"),
        freshness_config=custom,
    )

    payload = workbench.build(account_alias="live-main")

    assert payload["freshness_hints"]["tradability"]["stale_after_seconds"] == 1
    assert payload["freshness_hints"]["tradability"]["stale_threshold_seconds"] == 3
    # 未覆盖块保留默认
    assert payload["freshness_hints"]["risk"]["stale_after_seconds"] == 5
