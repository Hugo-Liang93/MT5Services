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
    assert comment_matches_semantics("M5:sma_trend:limit_rsig1", "M5", "sma_trend") is True
    assert looks_like_system_trade_comment(canonical) is True
    assert looks_like_system_trade_comment("agent:consensus:buy:sig_1") is True
    assert looks_like_system_trade_comment("manual_trade") is False


def test_compact_comment_label_preserves_minimal_readability() -> None:
    assert compact_comment_label("htf_h4_pullback") == "htfh4pu"
    assert compact_comment_label("supertrend") == "supertre"
