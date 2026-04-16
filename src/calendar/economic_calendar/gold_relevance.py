"""XAUUSD 事件相关性判定 —— 契约化模块。

## 模块契约

- `EventSummary`：事件相关性判定的标准输入 DTO。消费方必须提供完整字段，
  不接受 None 代替 name。
- `GoldRelevancePolicy`：判定策略的不可变值对象，从配置显式构造，含关键词与类别。
- `EventRelevanceMatcher`：判定协议（Protocol）。实现类对 `EventSummary` 返回 bool。
- `build_relevance_matcher(policy)`：工厂。policy 为空时 raise —— 调用方必须在
  构造前判断 `policy.is_empty()`，显式决定"不过滤"或"报错"，不存在静默兼容。

## 设计纪律

- 无 `getattr(..., default)` 兜底：字段缺失是上游 bug，不做默认值防御
- 无顶层便利函数：单一入口 = matcher 协议
- 无二元降级：空配置 ≠ 全通过；调用方显式决定行为
- 关键词匹配使用 word-boundary 正则（软边界避免子串误判）
- 连字符/空格归一化为单一字符类（"Non-Farm" ↔ "Non Farm"）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class EventSummary:
    """事件相关性判定的最小输入 DTO。

    字段：
      name:     事件名称（必填，非空字符串）
      category: DB category 字段（可空，但存在时不得为空串）
    """

    name: str
    category: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("EventSummary.name must be a non-empty string")
        if self.category is not None and (
            not isinstance(self.category, str) or not self.category
        ):
            raise ValueError(
                "EventSummary.category must be None or a non-empty string"
            )


@dataclass(frozen=True)
class GoldRelevancePolicy:
    """XAUUSD 事件相关性判定策略。

    不可变值对象。字段为"已清洗、已小写化、已去重"的元组——
    构造通常通过 `from_csv()` 工厂方法，确保输入一次性标准化。

    字段：
      keywords:   事件名 word-boundary 正则匹配项（小写）
      categories: DB category 字段子串匹配项（小写）
    """

    keywords: Tuple[str, ...]
    categories: Tuple[str, ...]

    @classmethod
    def from_csv(
        cls, *, keywords_csv: str, categories_csv: str
    ) -> "GoldRelevancePolicy":
        """从逗号分隔字符串构造。CSV 解析 + 清洗 + 去重集中在此处。"""
        return cls(
            keywords=_parse_csv(keywords_csv),
            categories=_parse_csv(categories_csv),
        )

    def is_empty(self) -> bool:
        """判断两维度是否均无配置。调用方需在构造 matcher 前显式检查。"""
        return not self.keywords and not self.categories


class EventRelevanceMatcher(Protocol):
    """事件相关性判定协议。"""

    def is_relevant(self, event: EventSummary) -> bool:
        ...


def build_relevance_matcher(
    policy: GoldRelevancePolicy,
) -> EventRelevanceMatcher:
    """构造匹配器。

    Raises:
        ValueError: policy.is_empty() —— 调用方必须在构造前显式判断是否构造 matcher。
    """
    if policy.is_empty():
        raise ValueError(
            "GoldRelevancePolicy is empty; caller must decide whether to skip "
            "relevance filtering rather than rely on a silent pass-through."
        )
    return _KeywordCategoryMatcher(policy)


class _KeywordCategoryMatcher:
    """基于 word-boundary 关键词正则 + category 子串白名单的 matcher 实现。"""

    __slots__ = ("_keyword_pattern", "_categories")

    def __init__(self, policy: GoldRelevancePolicy) -> None:
        self._keyword_pattern = _compile_keyword_pattern(policy.keywords)
        self._categories = policy.categories

    def is_relevant(self, event: EventSummary) -> bool:
        # 类别白名单：event.category 命中任一项（子串匹配）→ 相关
        if self._categories and event.category is not None:
            cat_lower = event.category.lower()
            for c in self._categories:
                if c in cat_lower:
                    return True
        # 关键词正则：event.name 命中 word-boundary 模式 → 相关
        if self._keyword_pattern is not None:
            if self._keyword_pattern.search(event.name):
                return True
        return False


# ── 内部辅助 ────────────────────────────────────────────────────────────────


def _parse_csv(raw: str) -> Tuple[str, ...]:
    """CSV → 清洗后 lower-case 元组（去空项 + 去重，保持首次出现顺序）。"""
    if not raw:
        return ()
    items = [s.strip().lower() for s in raw.split(",")]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return tuple(ordered)


def _compile_keyword_pattern(
    keywords: Sequence[str],
) -> Optional[re.Pattern[str]]:
    r"""将关键词列表编译为单个 word-boundary 正则。

    - 不区分大小写
    - 连字符/空格归一化为 `[-\s]+`（"Non-Farm" 也能命中 "Non Farm"）
    - 软 word-boundary `(?<![A-Za-z])...(?![A-Za-z])`：
      避免 "Fed" 匹配到 "Federal" 前缀，同时允许 "Fed Chair" 被命中
    """
    if not keywords:
        return None
    parts: list[str] = []
    for kw in keywords:
        escaped = re.escape(kw)
        escaped = re.sub(r"\\-|\\\s+", r"[-\\s]+", escaped)
        parts.append(escaped)
    pattern = r"(?<![A-Za-z])(?:" + "|".join(parts) + r")(?![A-Za-z])"
    return re.compile(pattern, re.IGNORECASE)
