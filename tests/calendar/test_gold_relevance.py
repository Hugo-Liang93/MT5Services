"""gold_relevance 契约测试。

覆盖：
  - EventSummary 输入校验
  - GoldRelevancePolicy.from_csv 清洗 + 去重 + 空判定
  - build_relevance_matcher 的契约：空 policy raise
  - 匹配器行为：word-boundary、hyphen/space 归一化、category 子串
"""

from __future__ import annotations

import pytest

from src.calendar.economic_calendar.gold_relevance import (
    EventSummary,
    GoldRelevancePolicy,
    build_relevance_matcher,
)


KEYWORDS = "Fed,NFP,FOMC,CPI,Non-Farm,Core PCE,Powell"
CATEGORIES = "Central Bank,Inflation,Employment"


def _matcher(keywords: str = KEYWORDS, categories: str = CATEGORIES):
    policy = GoldRelevancePolicy.from_csv(
        keywords_csv=keywords, categories_csv=categories
    )
    return build_relevance_matcher(policy)


class TestEventSummaryContract:
    def test_valid_minimal(self) -> None:
        ev = EventSummary(name="US NFP")
        assert ev.name == "US NFP"
        assert ev.category is None

    def test_valid_with_category(self) -> None:
        ev = EventSummary(name="US NFP", category="Employment")
        assert ev.category == "Employment"

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError):
            EventSummary(name="")

    def test_none_name_raises(self) -> None:
        with pytest.raises(ValueError):
            EventSummary(name=None)  # type: ignore[arg-type]

    def test_empty_category_raises(self) -> None:
        with pytest.raises(ValueError):
            EventSummary(name="X", category="")


class TestGoldRelevancePolicy:
    def test_from_csv_normalizes(self) -> None:
        p = GoldRelevancePolicy.from_csv(
            keywords_csv="NFP, FOMC , cpi, NFP",  # 含空格、大小写、重复
            categories_csv="Central Bank,Inflation",
        )
        # 清洗 + 小写化 + 去重
        assert p.keywords == ("nfp", "fomc", "cpi")
        assert p.categories == ("central bank", "inflation")

    def test_is_empty_true(self) -> None:
        p = GoldRelevancePolicy.from_csv(keywords_csv="", categories_csv="")
        assert p.is_empty()

    def test_is_empty_false(self) -> None:
        p = GoldRelevancePolicy.from_csv(keywords_csv="NFP", categories_csv="")
        assert not p.is_empty()


class TestBuildMatcherContract:
    def test_empty_policy_raises(self) -> None:
        empty = GoldRelevancePolicy.from_csv(keywords_csv="", categories_csv="")
        with pytest.raises(ValueError):
            build_relevance_matcher(empty)

    def test_nonempty_policy_ok(self) -> None:
        p = GoldRelevancePolicy.from_csv(keywords_csv="NFP", categories_csv="")
        m = build_relevance_matcher(p)
        assert m.is_relevant(EventSummary(name="US NFP"))


class TestWordBoundary:
    def test_fed_matches_fed_chair(self) -> None:
        m = _matcher()
        assert m.is_relevant(EventSummary(name="Fed Chair Speech"))

    def test_fed_does_not_match_federal_prefix(self) -> None:
        m = _matcher()
        assert not m.is_relevant(
            EventSummary(name="Federal Reserve of India Speech")
        )

    def test_nfp_boundary(self) -> None:
        m = _matcher()
        assert m.is_relevant(EventSummary(name="US NFP Preliminary"))
        assert not m.is_relevant(EventSummary(name="nfpacorn yield"))


class TestHyphenSpaceNormalization:
    def test_space_variant(self) -> None:
        assert _matcher().is_relevant(EventSummary(name="Non Farm Payrolls"))

    def test_hyphen_variant(self) -> None:
        assert _matcher().is_relevant(EventSummary(name="Non-Farm Payrolls"))

    def test_multiword_keyword_hyphen(self) -> None:
        assert _matcher().is_relevant(EventSummary(name="US Core PCE Index"))
        assert _matcher().is_relevant(EventSummary(name="US Core-PCE Index"))


class TestCategoryWhitelist:
    def test_category_bypasses_keyword_miss(self) -> None:
        m = _matcher()
        assert m.is_relevant(
            EventSummary(name="Obscure Event X", category="Central Bank")
        )

    def test_category_substring(self) -> None:
        m = _matcher()
        assert m.is_relevant(
            EventSummary(name="Report", category="US Employment Statistics")
        )

    def test_category_miss_falls_back_to_keyword(self) -> None:
        m = _matcher()
        # category 不命中 + 名称命中 → 相关
        assert m.is_relevant(
            EventSummary(name="US NFP", category="Agriculture")
        )

    def test_both_miss(self) -> None:
        m = _matcher()
        assert not m.is_relevant(
            EventSummary(
                name="New Zealand Dairy Auction", category="Agriculture"
            )
        )

    def test_category_case_insensitive(self) -> None:
        m = _matcher()
        assert m.is_relevant(
            EventSummary(name="Event", category="CENTRAL BANK")
        )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("US Non-Farm Payrolls", True),
        ("FOMC Statement", True),
        ("Powell Testimony", True),
        ("Jackson Hole Symposium (Powell)", True),
        ("New Zealand Dairy Auction", False),
        ("Japan Tankan Manufacturing Index", False),
        ("Beige Book Release", False),  # 未在测试 keywords 中
    ],
)
def test_realworld_event_names(name: str, expected: bool) -> None:
    assert _matcher().is_relevant(EventSummary(name=name)) is expected
