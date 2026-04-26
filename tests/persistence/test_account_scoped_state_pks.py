from __future__ import annotations

import re

from src.persistence.schema.pending_order_states import (
    DDL as PENDING_ORDER_STATES_DDL,
    UPSERT_SQL as UPSERT_PENDING_ORDER_STATES_SQL,
)
from src.persistence.schema.position_runtime_states import (
    DDL as POSITION_RUNTIME_STATES_DDL,
    UPSERT_SQL as UPSERT_POSITION_RUNTIME_STATES_SQL,
)


# ── §0u P1 回归：ticket 不能作全局主键，多账户下相互覆盖 ──


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def test_pending_order_states_pk_includes_account_scope() -> None:
    """P1 §0u 回归：order_ticket 单列主键 → 多账户同 MT5 ticket 互相覆盖。
    PK 必须包含账户维度（account_key 或 account_alias）。
    """
    ddl = _normalize(PENDING_ORDER_STATES_DDL)
    # order_ticket 不再单独标 PRIMARY KEY
    assert "order_ticket bigint primary key" not in ddl, (
        "PK 不能仅是 order_ticket（多账户会互相覆盖）"
    )
    # 必须有显式复合 PRIMARY KEY 包含 order_ticket 与账户列
    assert re.search(
        r"primary key\s*\(\s*account_(key|alias)\s*,\s*order_ticket\s*\)",
        ddl,
    ), f"DDL 必须含 PRIMARY KEY (account_*, order_ticket)；ddl={ddl!r}"


def test_pending_order_states_upsert_conflict_target_includes_account_scope() -> None:
    """ON CONFLICT target 必须与新 PK 一致，否则 upsert 仍会跨账户覆盖。"""
    sql = _normalize(UPSERT_PENDING_ORDER_STATES_SQL)
    assert "on conflict (order_ticket)" not in sql, (
        "ON CONFLICT 不能仅是 (order_ticket)"
    )
    assert re.search(
        r"on conflict\s*\(\s*account_(key|alias)\s*,\s*order_ticket\s*\)",
        sql,
    ), f"UPSERT 必须含 ON CONFLICT (account_*, order_ticket)；sql={sql!r}"


def test_position_runtime_states_pk_includes_account_scope() -> None:
    """同 P1 §0u 回归：position_ticket 不能作全局主键。"""
    ddl = _normalize(POSITION_RUNTIME_STATES_DDL)
    assert "position_ticket bigint primary key" not in ddl, (
        "PK 不能仅是 position_ticket（多账户会互相覆盖）"
    )
    assert re.search(
        r"primary key\s*\(\s*account_(key|alias)\s*,\s*position_ticket\s*\)",
        ddl,
    ), f"DDL 必须含 PRIMARY KEY (account_*, position_ticket)；ddl={ddl!r}"


def test_position_runtime_states_upsert_conflict_target_includes_account_scope() -> None:
    sql = _normalize(UPSERT_POSITION_RUNTIME_STATES_SQL)
    assert "on conflict (position_ticket)" not in sql, (
        "ON CONFLICT 不能仅是 (position_ticket)"
    )
    assert re.search(
        r"on conflict\s*\(\s*account_(key|alias)\s*,\s*position_ticket\s*\)",
        sql,
    ), f"UPSERT 必须含 ON CONFLICT (account_*, position_ticket)；sql={sql!r}"
