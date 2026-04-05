"""TimescaleDB retention & compression policy configuration.

Idempotent: ``remove_retention_policy`` + ``add_retention_policy`` ensures
the latest interval is applied.  Compression policies follow the same
pattern.  Safe to call on every startup.

Policy design:
  - **L1 (audit/trade)**: long retention (1-2 years), compress after 30 days
  - **L2 (signal/indicator)**: medium retention (90-180 days), compress after 7 days
  - **L3 (tick/quote/preview/trace)**: short retention (7-90 days), compress after 3-7 days
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionSpec:
    """Retention + compression spec for a single hypertable."""

    table: str
    retention_days: int
    compress_after_days: Optional[int]  # None = no compression
    segment_by: str  # compression segmentby columns
    order_by: str  # compression orderby column


# ── 默认 policy 配置（可通过 db.ini [retention] 覆盖天数） ───────────────
DEFAULT_POLICIES: list[RetentionSpec] = [
    # ── L3: 高频行情（短保留，激进压缩） ──
    RetentionSpec("ticks",                  retention_days=90,  compress_after_days=3,  segment_by="symbol",                  order_by="time DESC"),
    RetentionSpec("quotes",                 retention_days=90,  compress_after_days=3,  segment_by="symbol",                  order_by="time DESC"),
    RetentionSpec("ohlc_intrabar",          retention_days=30,  compress_after_days=3,  segment_by="symbol, timeframe",       order_by="recorded_at DESC"),

    # ── L3: 预览/诊断（短保留） ──
    RetentionSpec("signal_preview_events",  retention_days=7,   compress_after_days=3,  segment_by="symbol, timeframe",       order_by="generated_at DESC"),
    RetentionSpec("pipeline_trace_events",  retention_days=30,  compress_after_days=7,  segment_by="symbol, timeframe",       order_by="recorded_at DESC"),

    # ── L2: 信号/指标结果（中等保留） ──
    RetentionSpec("ohlc",                   retention_days=365, compress_after_days=30, segment_by="symbol, timeframe",       order_by="time DESC"),
    RetentionSpec("signal_events",          retention_days=180, compress_after_days=14, segment_by="symbol, timeframe",       order_by="generated_at DESC"),
    RetentionSpec("signal_outcomes",        retention_days=180, compress_after_days=14, segment_by="symbol, timeframe",       order_by="recorded_at DESC"),
    RetentionSpec("auto_executions",        retention_days=180, compress_after_days=14, segment_by="symbol",                  order_by="executed_at DESC"),

    # ── L2: 经济日历 ──
    RetentionSpec("economic_calendar_events",       retention_days=365, compress_after_days=30, segment_by="country",                   order_by="scheduled_at DESC"),
    RetentionSpec("economic_calendar_event_updates", retention_days=90,  compress_after_days=14, segment_by="event_uid",                 order_by="recorded_at DESC"),
    RetentionSpec("economic_event_market_impact",    retention_days=180, compress_after_days=30, segment_by="symbol, event_uid",         order_by="recorded_at DESC"),

    # ── L1: 交易审计（长保留，晚压缩） ──
    RetentionSpec("trade_outcomes",         retention_days=730, compress_after_days=30, segment_by="symbol, strategy",        order_by="recorded_at DESC"),
    RetentionSpec("trade_command_audits",   retention_days=730, compress_after_days=30, segment_by="account_alias",           order_by="recorded_at DESC"),
    RetentionSpec("circuit_breaker_history", retention_days=365, compress_after_days=30, segment_by="account_alias",          order_by="recorded_at DESC"),
    RetentionSpec("position_sl_tp_history", retention_days=365, compress_after_days=30, segment_by="symbol",                  order_by="recorded_at DESC"),
]


def apply_retention_policies(
    cursor,
    *,
    override_days: Optional[dict[str, int]] = None,
) -> dict[str, str]:
    """Apply retention + compression policies to all hypertables.

    Idempotent: drops existing policy before adding new one.

    Args:
        cursor: psycopg2 cursor (with autocommit connection)
        override_days: {table_name: retention_days} overrides from config

    Returns:
        dict of {table_name: status_message}
    """
    overrides = override_days or {}
    results: dict[str, str] = {}

    for spec in DEFAULT_POLICIES:
        table = spec.table
        retention = overrides.get(table, spec.retention_days)

        try:
            # ── Retention policy ──
            # 先移除旧 policy（幂等）
            cursor.execute(
                "SELECT remove_retention_policy(%s, if_exists => true)",
                (table,),
            )
            cursor.execute(
                "SELECT add_retention_policy(%s, INTERVAL %s)",
                (table, f"{retention} days"),
            )

            # ── Compression policy ──
            if spec.compress_after_days is not None:
                compress_days = spec.compress_after_days
                # 确保压缩时间不超过保留时间
                if compress_days >= retention:
                    compress_days = max(1, retention // 2)

                # 启用压缩（如果尚未启用）
                try:
                    cursor.execute(
                        f"ALTER TABLE {table} SET ("
                        f"  timescaledb.compress,"
                        f"  timescaledb.compress_segmentby = '{spec.segment_by}',"
                        f"  timescaledb.compress_orderby = '{spec.order_by}'"
                        f")"
                    )
                except Exception:
                    # 压缩设置已存在时可能报错，安全跳过
                    logger.debug("Compression settings already exist for %s", table)

                # 先移除旧 compression policy（幂等）
                cursor.execute(
                    "SELECT remove_compression_policy(%s, if_exists => true)",
                    (table,),
                )
                cursor.execute(
                    "SELECT add_compression_policy(%s, INTERVAL %s)",
                    (table, f"{compress_days} days"),
                )
                results[table] = f"retention={retention}d, compress={compress_days}d"
            else:
                results[table] = f"retention={retention}d, no compression"

            logger.debug("Retention policy applied: %s → %s", table, results[table])

        except Exception as exc:
            results[table] = f"FAILED: {exc}"
            logger.warning("Failed to apply retention policy for %s: %s", table, exc)

    return results
