from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

MAX_MT5_COMMENT_LENGTH = 27
_COMMENT_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")

# 策略 → MT5 broker comment alias（≤8 chars）。
#
# 历史 token-split 算法（_legacy_compact_label）让所有 structured_* 策略的
# comment label 都以 "str" 起头浪费字符，区分度差（strtrco / strtrh4 / strstrt
# 难辨）。alias 直接命名缩写，可读性好且都 ≤4 chars，给 scope/action/request_tag
# 留出更多预算。
#
# 新增策略**必须**在此登记，否则 validate_strategy_aliases() 在装配阶段 raise
# 阻止启动（fail-fast 强契约，参 Q2=B）。
_STRATEGY_ALIAS: Mapping[str, str] = {
    "structured_trend_continuation": "tc",
    "structured_trend_h4": "th4",
    "structured_trend_h4_momentum": "th4m",
    "structured_sweep_reversal": "swp",
    "structured_breakout_follow": "bof",
    "structured_range_reversion": "rr",
    "structured_session_breakout": "sbo",
    "structured_trendline_touch": "tlt",
    "structured_lowbar_entry": "lbe",
    "structured_pullback_window": "pbw",
    "structured_open_range_breakout": "orb",
    "structured_price_action": "pa",
    "structured_regime_exhaustion": "rex",
    "structured_strong_trend_follow": "stf",
}
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
    """策略 → broker comment label。

    优先查 _STRATEGY_ALIAS 表（命中即返回 alias，已规范小写、≤8）；未登记
    策略走 _legacy_compact_label 老 token-split 算法（向后兼容）。

    Q3 双格式判等：comment_matches_semantics 对老持仓的 comment 同时尝试
    新 alias 和老 hash，避免新代码上线后历史 comment reconcile 失配。
    """
    raw = str(value or "")
    alias = _STRATEGY_ALIAS.get(raw)
    if alias is not None:
        return alias[:max_length]
    return _legacy_compact_label(raw, max_length=max_length)


def _legacy_compact_label(value: str, *, max_length: int = 8) -> str:
    """老 token-split 算法：拆词后取首词 3 char + 后续每词 2 char。

    保留作向后兼容 fallback——历史持仓 comment 用此算法生成（如
    "strtrco" / "strpuwi"），位置 reconcile 必须能继续识别。
    """
    tokens = [
        token for token in _COMMENT_TOKEN_RE.split(str(value or "").lower()) if token
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


def validate_strategy_aliases(strategy_names: Iterable[str]) -> None:
    """启动期校验：所有 catalog 策略都必须在 _STRATEGY_ALIAS 登记。

    Q2=B fail-fast 强契约——避免新增策略时遗漏 alias 注册导致悄悄走老
    hash 算法（区分度差、占字符）。alias 唯一性也在此校验，防 broker
    端 comment 解析 → 策略反查时撞名。

    Raises:
        ValueError: 未登记策略 / alias 重复。
    """
    missing = sorted(
        name
        for name in strategy_names
        if str(name).strip() and str(name).strip() not in _STRATEGY_ALIAS
    )
    if missing:
        raise ValueError(
            "Strategies missing broker comment alias: "
            + ", ".join(missing)
            + ". Add entries to _STRATEGY_ALIAS in src/trading/broker/comment_codec.py."
        )

    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str, str]] = []
    for name, alias in _STRATEGY_ALIAS.items():
        existing = seen.get(alias)
        if existing is not None:
            duplicates.append((alias, existing, name))
        else:
            seen[alias] = name
    if duplicates:
        msg = "; ".join(
            f"alias={a!r} used by both {n1!r} and {n2!r}" for a, n1, n2 in duplicates
        )
        raise ValueError(f"Duplicate broker comment aliases detected: {msg}")


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
        kind_code = cleaned[:1] or "m"
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
    """判 broker comment 是否对应给定 (timeframe, strategy)。

    Q3 双格式兼容：新 comment 用 alias label（如 "tc"），但仓位 reconcile
    时也会遇到老 comment（用 _legacy_compact_label 生成的 hash 如 "strtrco"）。
    两个 label 算法都尝试匹配，任一命中即视为同一策略。
    """
    parsed = parse_trade_comment(comment)
    if parsed is not None:
        if parsed.scope != _normalize_scope(timeframe):
            return False
        new_label = compact_comment_label(strategy)
        if parsed.label == new_label:
            return True
        legacy_label = _legacy_compact_label(strategy)
        if legacy_label != new_label and parsed.label == legacy_label:
            return True
        return False
    legacy_prefix = (
        f"{str(timeframe or '').strip().lower()}:{str(strategy or '').strip().lower()}:"
    )
    return bool(
        legacy_prefix and str(comment or "").strip().lower().startswith(legacy_prefix)
    )


def looks_like_system_trade_comment(comment: str) -> bool:
    raw = str(comment or "").strip()
    if not raw:
        return False
    if parse_trade_comment(raw) is not None:
        return True
    normalized = raw.lower()
    return any(normalized.startswith(prefix) for prefix in _LEGACY_RESTORABLE_PREFIXES)
