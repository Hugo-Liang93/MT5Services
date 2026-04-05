"""Shared SQLite connection factory with WAL mode and tuned PRAGMAs.

All SQLite databases in the project should use this factory to ensure
consistent WAL configuration and avoid duplicated PRAGMA setup code.
"""

from __future__ import annotations

import sqlite3


def make_sqlite_conn(
    db_path: str,
    *,
    cache_mb: int = 8,
    busy_timeout_ms: int = 30000,
) -> sqlite3.Connection:
    """Create a persistent SQLite connection with WAL mode and performance PRAGMAs.

    Args:
        db_path: Path to the SQLite database file.
        cache_mb: Page cache size in MB (default 8).
        busy_timeout_ms: Busy-wait timeout in milliseconds (default 30000).

    Returns:
        Configured ``sqlite3.Connection`` (check_same_thread=False for cross-thread use).
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")
    return conn
