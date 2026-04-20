"""SSE envelope 字段语义测试（P9 Phase 2.1）。

验证 ``next_stream_envelope`` 输出 schema 1.1 的新字段：
- ``tier``：T0 / T1 / T2 / T3 / static（对齐 docs/design/quantx-data-freshness-tiering.md）
- ``entity_scope``：account / symbol / system
- ``changed_blocks``：精确指明前端需要重拉的 workbench 块
- ``snapshot_version``：与 state_version 同值，用于 workbench 对账
"""

from __future__ import annotations

import json

from src.api.trade_routes.state_routes.stream import (
    _EVENT_METADATA,
    _PIPELINE_DEFAULT_METADATA,
    _resolve_event_metadata,
    format_trade_state_sse,
    next_stream_envelope,
)


def _new_counters() -> dict[str, int]:
    return {"sequence": 0, "state_version": 0}


# ── envelope 顶层字段 ─────────────────────────────────────────────


def test_envelope_uses_schema_v1_1_for_new_fields() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="live-main",
        event_type="position_changed",
        payload={},
    )
    assert env["schema_version"] == "1.1"
    for field in ("tier", "entity_scope", "changed_blocks", "snapshot_version"):
        assert field in env, f"missing field: {field}"


def test_envelope_snapshot_version_matches_state_version() -> None:
    counters = _new_counters()
    env_a = next_stream_envelope(
        counters, account="x", event_type="position_changed", payload={}
    )
    env_b = next_stream_envelope(
        counters, account="x", event_type="order_changed", payload={}
    )
    assert env_a["snapshot_version"] == env_a["state_version"] == 1
    assert env_b["snapshot_version"] == env_b["state_version"] == 2


def test_envelope_state_change_false_keeps_snapshot_version_stable() -> None:
    counters = _new_counters()
    env_first = next_stream_envelope(
        counters, account="x", event_type="position_changed", payload={}
    )
    env_heartbeat = next_stream_envelope(
        counters,
        account="x",
        event_type="heartbeat",
        payload={"healthy": True},
        state_change=False,
    )
    assert env_heartbeat["snapshot_version"] == env_first["snapshot_version"] == 1
    # sequence 仍递增（用于事件排序），state_version 不变
    assert env_heartbeat["sequence"] == 2
    assert env_heartbeat["state_version"] == 1


# ── 自动 metadata 查表 ────────────────────────────────────────────


def test_position_changed_metadata_is_tier_t1_symbol_scope() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="position_changed",
        payload={},
        symbol="XAUUSD",
    )
    assert env["tier"] == "T1"
    assert env["entity_scope"] == "symbol"
    assert env["changed_blocks"] == ["positions", "exposure"]


def test_runtime_mode_changed_metadata_is_tier_t0_system_scope() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="runtime_mode_changed",
        payload={},
    )
    assert env["tier"] == "T0"
    assert env["entity_scope"] == "system"
    assert env["changed_blocks"] == ["execution"]


def test_risk_state_changed_marks_execution_and_risk_blocks() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="risk_state_changed",
        payload={},
    )
    assert env["tier"] == "T0"
    assert env["entity_scope"] == "account"
    assert set(env["changed_blocks"]) == {"execution", "risk"}


def test_closeout_finished_changes_four_blocks() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="closeout_finished",
        payload={},
    )
    assert env["tier"] == "T1"
    assert set(env["changed_blocks"]) == {
        "execution",
        "positions",
        "orders",
        "exposure",
    }


def test_command_audit_appended_marks_events_and_related_objects() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="command_audit_appended",
        payload={},
    )
    assert env["tier"] == "T1"
    assert env["entity_scope"] == "account"
    assert set(env["changed_blocks"]) == {"events", "relatedObjects"}


def test_state_snapshot_includes_all_workbench_blocks() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="state_snapshot",
        payload={},
    )
    assert env["tier"] == "T0"
    # 与 /v1/execution/workbench 9 块（含 stream 元数据）对齐
    expected = {
        "execution",
        "risk",
        "positions",
        "orders",
        "pending",
        "exposure",
        "events",
        "relatedObjects",
        "marketContext",
        "stream",
    }
    assert set(env["changed_blocks"]) == expected


def test_resync_required_marks_changed_blocks_empty() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="resync_required",
        payload={"reason": "buffer_missed"},
    )
    assert env["tier"] == "T0"
    assert env["changed_blocks"] == []  # 前端必须全量重拉


def test_heartbeat_metadata_is_static_tier_system_scope() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="heartbeat",
        payload={"healthy": True},
        state_change=False,
    )
    assert env["tier"] == "static"
    assert env["entity_scope"] == "system"
    assert env["changed_blocks"] == []


# ── pipeline 透传 fallback ────────────────────────────────────────


def test_unknown_event_type_falls_back_to_pipeline_default_metadata() -> None:
    """command_submitted / intent_published 等 pipeline 透传事件未在 _EVENT_METADATA
    表中显式注册，应 fallback 到 ('T3', 'account', ('events',))。"""
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="intent_published",
        payload={},
    )
    assert env["tier"] == "T3"
    assert env["entity_scope"] == "account"
    assert env["changed_blocks"] == ["events"]


def test_resolve_event_metadata_returns_pipeline_default_for_unknown() -> None:
    assert (
        _resolve_event_metadata("totally_unknown_event") == _PIPELINE_DEFAULT_METADATA
    )


# ── 显式覆盖参数 ──────────────────────────────────────────────────


def test_explicit_tier_override_wins_over_table_lookup() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="position_changed",
        payload={},
        tier="T0",  # 强制升级 tier，例如紧急风控通知场景
    )
    assert env["tier"] == "T0"
    # 其他字段仍走默认查表
    assert env["entity_scope"] == "symbol"


def test_explicit_changed_blocks_override_replaces_default() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="position_changed",
        payload={},
        changed_blocks=["positions"],  # 不刷 exposure（如已知没改）
    )
    assert env["changed_blocks"] == ["positions"]


def test_explicit_changed_blocks_empty_list_is_respected() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="position_changed",
        payload={},
        changed_blocks=[],
    )
    assert env["changed_blocks"] == []


def test_explicit_entity_scope_override_wins() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="x",
        event_type="position_changed",
        payload={},
        entity_scope="account",
    )
    assert env["entity_scope"] == "account"


# ── SSE 序列化端到端 ──────────────────────────────────────────────


def test_format_trade_state_sse_serializes_new_fields() -> None:
    env = next_stream_envelope(
        _new_counters(),
        account="live-main",
        event_type="position_changed",
        payload={"ticket": 1},
        symbol="XAUUSD",
    )
    sse = format_trade_state_sse(env)
    # 抽出 data: 行解析回 JSON 验证完整透出
    data_line = next(line for line in sse.split("\n") if line.startswith("data: "))
    parsed = json.loads(data_line[len("data: ") :])
    assert parsed["tier"] == "T1"
    assert parsed["entity_scope"] == "symbol"
    assert parsed["changed_blocks"] == ["positions", "exposure"]
    assert parsed["snapshot_version"] == parsed["state_version"]
    assert parsed["schema_version"] == "1.1"


# ── metadata 表完整性 ────────────────────────────────────────────


def test_event_metadata_table_uses_known_tier_values() -> None:
    """所有注册 event 的 tier 必须落在已定义集合中。"""
    valid_tiers = {"T0", "T1", "T2", "T3", "T4", "static"}
    for event_type, (tier, _scope, _blocks) in _EVENT_METADATA.items():
        assert tier in valid_tiers, f"{event_type} has invalid tier: {tier}"


def test_event_metadata_table_uses_known_scope_values() -> None:
    valid_scopes = {"account", "symbol", "system"}
    for event_type, (_tier, scope, _blocks) in _EVENT_METADATA.items():
        assert scope in valid_scopes, f"{event_type} has invalid scope: {scope}"


def test_event_metadata_table_uses_known_workbench_blocks() -> None:
    """changed_blocks 内的字符串必须对齐 /v1/execution/workbench 9 块名。"""
    valid_blocks = {
        "execution",
        "risk",
        "positions",
        "orders",
        "pending",
        "exposure",
        "events",
        "relatedObjects",
        "marketContext",
        "stream",
    }
    for event_type, (_tier, _scope, blocks) in _EVENT_METADATA.items():
        for block in blocks:
            assert (
                block in valid_blocks
            ), f"{event_type} references unknown block: {block}"
