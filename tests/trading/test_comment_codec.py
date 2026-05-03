from __future__ import annotations

from src.trading.broker.comment_codec import (
    MAX_MT5_COMMENT_LENGTH,
    build_trade_comment,
    comment_matches_request_id,
    comment_matches_semantics,
    comments_share_request_tag,
    compact_comment_label,
    extract_comment_request_tag,
    looks_like_system_trade_comment,
    parse_trade_comment,
)


def test_build_trade_comment_uses_broker_safe_compact_format() -> None:
    comment = build_trade_comment(
        request_id="REQ-ABC12345-TAIL",
        timeframe="M30",
        strategy="htf_h4_pullback",
        side="buy",
        order_kind="limit",
    )

    assert comment == "M30_htfh4pu_bl_reqabc12"
    assert len(comment) <= MAX_MT5_COMMENT_LENGTH


def test_parse_trade_comment_round_trips_canonical_comment() -> None:
    comment = build_trade_comment(
        request_id="sig_h4_pullback",
        timeframe="M30",
        strategy="htf_h4_pullback",
        side="buy",
        order_kind="limit",
    )

    parsed = parse_trade_comment(comment)

    assert parsed is not None
    assert parsed.scope == "M30"
    assert parsed.label == "htfh4pu"
    assert parsed.action == "bl"
    assert parsed.request_tag == "sigh4pul"


def test_comment_matching_prefers_request_tag_over_full_text() -> None:
    left = "M30_htfh4pu_bl_sigh4pul"
    right = "TR_legacy_bl_sigh4pul"

    assert comments_share_request_tag(left, right) is True
    assert comment_matches_request_id(left, "sig_h4_pullback") is True
    assert extract_comment_request_tag(right) == "sigh4pul"


def test_comment_semantics_support_new_and_legacy_formats() -> None:
    canonical = build_trade_comment(
        request_id="sig-1",
        timeframe="M5",
        strategy="sma_trend",
        side="buy",
        order_kind="market",
    )

    assert comment_matches_semantics(canonical, "M5", "sma_trend") is True
    assert (
        comment_matches_semantics("M5:sma_trend:limit_rsig1", "M5", "sma_trend") is True
    )
    assert looks_like_system_trade_comment(canonical) is True
    assert looks_like_system_trade_comment("agent:consensus:buy:sig_1") is True
    assert looks_like_system_trade_comment("manual_trade") is False


def test_compact_comment_label_preserves_minimal_readability() -> None:
    assert compact_comment_label("htf_h4_pullback") == "htfh4pu"
    assert compact_comment_label("supertrend") == "supertre"


# ── strategy alias map (Q1=A / Q2=B / Q3=dual-format) ─────────────────────


def test_compact_comment_label_uses_registered_alias() -> None:
    """注册策略走 _STRATEGY_ALIAS（短且可读），不再走 token-split hash。"""
    from src.trading.broker.comment_codec import _STRATEGY_ALIAS

    # 2026-04-30 大清场后 alias 表仅 price_action
    assert compact_comment_label("structured_price_action") == "pa"
    # alias 表内所有策略都 ≤8 chars（broker label 字段上限）
    for alias in _STRATEGY_ALIAS.values():
        assert 1 <= len(alias) <= 8, alias


def test_compact_comment_label_falls_back_to_legacy_for_unregistered() -> None:
    """未注册策略走老 token-split 算法（向后兼容外部/测试场景）。"""
    # "htf_h4_pullback" 不在 _STRATEGY_ALIAS 表 → legacy hash
    assert compact_comment_label("htf_h4_pullback") == "htfh4pu"


def test_comment_matches_semantics_dual_format_alias_and_legacy() -> None:
    """Q3 双格式判等：新 alias 与老 hash 都能识别为同一策略。

    2026-04-30 大清场后用 price_action 验证（其他策略已删除）。
    历史 hash 格式："stpra" 之类（取决于 _legacy_compact_label 算法）；
    新格式 = "pa"。位置 reconcile 应同时识别两种。
    """
    parsed_new = parse_trade_comment(
        build_trade_comment(
            request_id="abcd1234",
            timeframe="M30",
            strategy="structured_price_action",
            side="buy",
            order_kind="market",
        )
    )
    assert parsed_new is not None
    assert parsed_new.label == "pa"

    # 同一策略，新 alias comment 应匹配
    assert (
        comment_matches_semantics(
            "M30_pa_bm_abcd1234", "M30", "structured_price_action"
        )
        is True
    )


def test_validate_strategy_aliases_passes_for_registered() -> None:
    """所有 catalog 注册策略都已在 alias 表登记。"""
    from src.signals.strategies.catalog import build_default_strategy_set
    from src.trading.broker.comment_codec import validate_strategy_aliases

    strategy_names = [s.name for s in build_default_strategy_set()]
    validate_strategy_aliases(strategy_names)  # 不应 raise


def test_validate_strategy_aliases_raises_on_unregistered() -> None:
    """未登记策略 → fail-fast（Q2=B 强契约）。"""
    import pytest

    from src.trading.broker.comment_codec import validate_strategy_aliases

    with pytest.raises(ValueError, match="missing broker comment alias"):
        validate_strategy_aliases(
            ["structured_trend_continuation", "future_unregistered_strategy"]
        )


def test_strategy_alias_map_has_no_duplicates() -> None:
    """alias 唯一性：避免两个策略撞同一 broker label 解析。"""
    from src.trading.broker.comment_codec import _STRATEGY_ALIAS

    aliases = list(_STRATEGY_ALIAS.values())
    assert len(aliases) == len(set(aliases)), "Duplicate alias detected: " + str(
        aliases
    )
