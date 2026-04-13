from __future__ import annotations

import re
from dataclasses import dataclass

MAX_MT5_COMMENT_LENGTH = 27
_COMMENT_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")
_LEGACY_REQUEST_TAG_RE = re.compile(r"_r([a-z0-9]{1,8})$", re.IGNORECASE)
_LEGACY_RESTORABLE_PREFIXES = (
    "auto:",
    "agent:",
    "m1:",
    "m5:",
    "m15:",
    "m30:",
    "h1:",
    "h4:",
    "d1:",
    "w1:",
    "mn1:",
)


@dataclass(frozen=True)
class ParsedTradeComment:
    raw: str
    scope: str
    label: str
    action: str
    request_tag: str


def normalize_request_tag(request_id: str, *, max_length: int = 8) -> str:
    normalized = "".join(ch for ch in str(request_id or "") if ch.isalnum()).lower()
    return normalized[: max(1, max_length)]


def compact_comment_label(value: str, *, max_length: int = 8) -> str:
    tokens = [
        token
        for token in _COMMENT_TOKEN_RE.split(str(value or "").lower())
        if token
    ]
    if not tokens:
        return "trade"[:max_length]
    if len(tokens) == 1:
        return tokens[0][:max_length]

    parts: list[str] = []
    remaining = max_length
    for index, token in enumerate(tokens):
        if remaining <= 0:
            break
        take = 3 if index == 0 else 2
        part = token[: min(take, remaining)]
        if not part:
            continue
        parts.append(part)
        remaining -= len(part)
    label = "".join(parts)
    if not label:
        label = "".join(tokens)[:max_length]
    return label[:max_length]


def _normalize_scope(timeframe: str) -> str:
    cleaned = "".join(ch for ch in str(timeframe or "").upper() if ch.isalnum())
    return (cleaned or "TR")[:3]


def _normalize_action(side: str, order_kind: str) -> str:
    normalized_side = str(side or "").strip().lower()
    normalized_kind = str(order_kind or "").strip().lower().replace("-", "_")
    side_code = {
        "buy": "b",
        "sell": "s",
    }.get(normalized_side, "t")
    kind_code = {
        "market": "m",
        "limit": "l",
        "stop": "s",
        "stop_limit": "p",
        "stoplimit": "p",
    }.get(normalized_kind)
    if kind_code is None:
        cleaned = "".join(ch for ch in normalized_kind if ch.isalnum())
        kind_code = (cleaned[:1] or "m")
    return f"{side_code}{kind_code}"


def build_trade_comment(
    *,
    request_id: str,
    timeframe: str = "",
    strategy: str = "",
    side: str = "",
    order_kind: str = "market",
    comment: str = "",
) -> str:
    parts = [
        _normalize_scope(timeframe),
        compact_comment_label(strategy or comment or "trade"),
        _normalize_action(side, order_kind),
        normalize_request_tag(request_id),
    ]
    return "_".join(part for part in parts if part)[:MAX_MT5_COMMENT_LENGTH]


def parse_trade_comment(comment: str) -> ParsedTradeComment | None:
    raw = str(comment or "").strip()
    if not raw:
        return None
    parts = raw.split("_")
    if len(parts) != 4:
        return None
    scope, label, action, request_tag = parts
    if len(scope) > 3 or len(label) > 8 or len(request_tag) > 8:
        return None
    normalized_tag = normalize_request_tag(request_tag, max_length=len(request_tag))
    if not scope or not label or len(action) != 2 or not normalized_tag:
        return None
    if normalized_tag != request_tag.lower():
        return None
    return ParsedTradeComment(
        raw=raw,
        scope=scope.upper(),
        label=label.lower(),
        action=action.lower(),
        request_tag=request_tag.lower(),
    )


def extract_comment_request_tag(comment: str) -> str:
    parsed = parse_trade_comment(comment)
    if parsed is not None:
        return parsed.request_tag
    raw = str(comment or "").strip().lower()
    if not raw:
        return ""
    legacy_match = _LEGACY_REQUEST_TAG_RE.search(raw)
    if legacy_match is not None:
        return legacy_match.group(1).lower()
    return ""


def comments_share_request_tag(left: str, right: str) -> bool:
    left_tag = extract_comment_request_tag(left)
    right_tag = extract_comment_request_tag(right)
    return bool(left_tag and right_tag and left_tag == right_tag)


def comment_matches_request_id(comment: str, request_id: str) -> bool:
    target_tag = normalize_request_tag(request_id)
    if not target_tag:
        return False
    return extract_comment_request_tag(comment) == target_tag


def comment_matches_semantics(comment: str, timeframe: str, strategy: str) -> bool:
    parsed = parse_trade_comment(comment)
    if parsed is not None:
        return (
            parsed.scope == _normalize_scope(timeframe)
            and parsed.label == compact_comment_label(strategy)
        )
    legacy_prefix = f"{str(timeframe or '').strip().lower()}:{str(strategy or '').strip().lower()}:"
    return bool(legacy_prefix and str(comment or "").strip().lower().startswith(legacy_prefix))


def looks_like_system_trade_comment(comment: str) -> bool:
    raw = str(comment or "").strip()
    if not raw:
        return False
    if parse_trade_comment(raw) is not None:
        return True
    normalized = raw.lower()
    return any(normalized.startswith(prefix) for prefix in _LEGACY_RESTORABLE_PREFIXES)
