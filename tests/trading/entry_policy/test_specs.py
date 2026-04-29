"""EntrySpecGroup / EntrySpecMember 值对象单元测试。"""

from __future__ import annotations

import pytest

from src.trading.entry_policy import (
    EntrySpecGroup,
    EntrySpecMember,
    EntryType,
    new_group_id,
)


def _make_member(
    member_id: str = "market", entry_type: EntryType = EntryType.MARKET
) -> EntrySpecMember:
    return EntrySpecMember(
        member_id=member_id,
        entry_type=entry_type,
        trigger_price=4500.0,
        entry_low=4500.0,
        entry_high=4500.0,
    )


class TestEntrySpecMember:
    def test_group_role_derived_from_entry_type(self):
        member = _make_member(entry_type=EntryType.LIMIT)
        assert member.group_role == "limit"

        member = _make_member(entry_type=EntryType.STOP)
        assert member.group_role == "stop"

    def test_frozen(self):
        member = _make_member()
        with pytest.raises(Exception):  # FrozenInstanceError
            member.trigger_price = 9999.0  # type: ignore[misc]


class TestEntrySpecGroup:
    def test_single_member_group(self):
        member = _make_member()
        group = EntrySpecGroup(group_id=new_group_id(), members=(member,))
        assert group.is_single_member is True
        assert group.is_oco is False
        assert group.all_market() is True
        assert len(group.group_id) == 32  # uuid4 hex

    def test_oco_group_two_members(self):
        a = _make_member(member_id="limit_pullback", entry_type=EntryType.LIMIT)
        b = _make_member(member_id="stop_breakout", entry_type=EntryType.STOP)
        group = EntrySpecGroup(group_id=new_group_id(), members=(a, b))
        assert group.is_oco is True
        assert group.is_single_member is False
        assert group.all_market() is False

    def test_empty_members_rejected(self):
        with pytest.raises(ValueError, match="must contain at least 1 member"):
            EntrySpecGroup(group_id=new_group_id(), members=())

    def test_duplicate_member_id_rejected(self):
        a = _make_member(member_id="x")
        b = _make_member(member_id="x")
        with pytest.raises(ValueError, match="duplicate member_id"):
            EntrySpecGroup(group_id=new_group_id(), members=(a, b))

    def test_to_dict_serializable(self):
        a = _make_member(member_id="a", entry_type=EntryType.LIMIT)
        b = _make_member(member_id="b", entry_type=EntryType.STOP)
        group = EntrySpecGroup(
            group_id="abcdef",
            members=(a, b),
            cancellation_policy="any_fill",
            metadata={"policy_name": "oco", "branch": "test"},
        )
        d = group.to_dict()
        assert d["group_id"] == "abcdef"
        assert d["cancellation_policy"] == "any_fill"
        assert d["metadata"]["policy_name"] == "oco"
        assert len(d["members"]) == 2
        assert d["members"][0]["entry_type"] == "limit"
        assert d["members"][1]["entry_type"] == "stop"

    def test_new_group_id_unique(self):
        ids = {new_group_id() for _ in range(100)}
        assert len(ids) == 100
