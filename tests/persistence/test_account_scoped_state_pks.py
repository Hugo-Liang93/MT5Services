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


# ── §0v P3 回归：trade_control_state PK 与 unique 索引冲突目标不一致 ──


def test_trade_control_state_pk_matches_unique_index_to_avoid_alias_rename_failure() -> None:
    """P3 §0v 回归：旧 schema PK=account_alias + UNIQUE INDEX=account_key →
    同 account_key 不同 alias（别名重命名 / 规范化）写入时 unique 索引先冲突，
    但 ON CONFLICT (account_alias) 接不住 → UPSERT 直接抛 UNIQUE violation。
    PK / 唯一约束 / ON CONFLICT 必须落在同一列上（应统一到 account_key）。
    """
    from src.persistence.schema.trade_control_state import (
        DDL as TRADE_CONTROL_STATE_DDL,
        UPSERT_SQL as UPSERT_TRADE_CONTROL_STATE_SQL,
    )

    ddl = _normalize(TRADE_CONTROL_STATE_DDL)
    upsert = _normalize(UPSERT_TRADE_CONTROL_STATE_SQL)

    # 旧 PK 形态 account_alias 单列必须移除（多账户 alias 不全局唯一 / 可重命名）
    assert "account_alias text primary key" not in ddl, (
        "PK 不能仅是 account_alias（不全局唯一/可重命名 → unique index 冲突写失败）"
    )
    # 新 PK 必须以 account_key 为锚（globally unique env:server:login）
    assert (
        "account_key text not null primary key" in ddl
        or re.search(r"primary key\s*\(\s*account_key\s*\)", ddl)
    ), f"DDL 必须以 account_key 为 PK；ddl={ddl!r}"

    # ON CONFLICT 必须与 PK 一致
    assert "on conflict (account_alias)" not in upsert, (
        "ON CONFLICT 不能仍写 account_alias，否则 alias 改名时 unique 索引"
        " 冲突 → ON CONFLICT 接不住 → 直接 UNIQUE violation"
    )
    assert re.search(
        r"on conflict\s*\(\s*account_key\s*\)",
        upsert,
    ), f"UPSERT 必须 ON CONFLICT (account_key)；upsert={upsert!r}"
