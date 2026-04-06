"""历史 OHLC 数据回填工具 — 从 MT5 批量下载历史 K 线写入 TimescaleDB。

用法：
    # 回填近 3 个月全部 TF（回测用）
    python tools/backfill_ohlc.py --start 2025-12-30 --end 2026-03-30

    # 回填近 6 个月 M5 和 H1
    python tools/backfill_ohlc.py --start 2025-09-01 --end 2026-03-30 --tf M5,H1

    # 回填指定品种
    python tools/backfill_ohlc.py --symbol XAUUSD --start 2025-01-01 --end 2026-04-05

    # 增量回填（从 DB 已有数据的最后一根 bar 继续）
    python tools/backfill_ohlc.py --incremental

    # 仅检查不写入
    python tools/backfill_ohlc.py --start 2025-12-30 --end 2026-03-30 --dry-run

    # 查看 DB 当前数据覆盖情况
    python tools/backfill_ohlc.py --status

功能：
    - 从 MT5 终端分批拉取历史 OHLC（batch_size 根/批）
    - upsert 写入 TimescaleDB（重复数据自动覆盖，安全可重跑）
    - 支持增量模式（从 DB 已有数据末尾继续）
    - 自动读取 mt5.local.ini + db.local.ini 配置
    - 进度实时输出

设计原则：
    - 复用 src/clients/mt5_market.py 的 MT5MarketClient（与实盘相同的连接/时间偏移逻辑）
    - 复用 src/persistence/db.py 的 TimescaleWriter（与实盘相同的写入/校验逻辑）
    - 不引入任何新的 MT5 或 DB 操作代码
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _get_db_coverage(writer: "TimescaleWriter", symbol: str, timeframes: List[str]) -> dict:
    """查询 DB 中各 TF 的数据覆盖情况。"""
    coverage = {}
    try:
        conn = writer._pool.getconn()
        cur = conn.cursor()
        for tf in timeframes:
            cur.execute(
                "SELECT COUNT(*), MIN(time), MAX(time) FROM ohlc "
                "WHERE symbol = %s AND timeframe = %s",
                (symbol, tf),
            )
            row = cur.fetchone()
            coverage[tf] = {
                "count": row[0] or 0,
                "first": row[1],
                "last": row[2],
            }
        cur.close()
        writer._pool.putconn(conn)
    except Exception as e:
        logger.error("Failed to query DB coverage: %s", e)
    return coverage


def _render_status(coverage: dict, symbol: str) -> str:
    """渲染数据覆盖状态表。"""
    lines = [
        f"\n{'='*65}",
        f" {symbol} OHLC Data Coverage in TimescaleDB",
        f"{'='*65}",
        f"  {'TF':<5} {'Bars':>8} {'First':>20} {'Last':>20}",
        "-" * 65,
    ]
    total = 0
    for tf, info in sorted(coverage.items()):
        count = info["count"]
        total += count
        first = info["first"].strftime("%Y-%m-%d %H:%M") if info["first"] else "---"
        last = info["last"].strftime("%Y-%m-%d %H:%M") if info["last"] else "---"
        lines.append(f"  {tf:<5} {count:>8} {first:>20} {last:>20}")
    lines.append("-" * 65)
    lines.append(f"  Total: {total} bars")
    return "\n".join(lines)


def backfill(
    symbol: str,
    timeframes: List[str],
    start: datetime,
    end: datetime,
    batch_size: int = 5000,
    dry_run: bool = False,
    incremental: bool = False,
) -> None:
    """从 MT5 拉取历史 OHLC 并写入 TimescaleDB。"""
    from src.clients.mt5_market import MT5MarketClient
    from src.config.database import load_db_settings
    from src.config.mt5 import load_mt5_settings
    from src.persistence.db import TimescaleWriter
    from src.utils.common import timeframe_seconds

    # 初始化 MT5 客户端
    mt5_settings = load_mt5_settings()
    client = MT5MarketClient(mt5_settings)
    logger.info("MT5 connected: server=%s login=%s", mt5_settings.mt5_server, mt5_settings.mt5_login)

    # 初始化 DB
    db_settings = load_db_settings()
    writer = TimescaleWriter(db_settings, min_conn=1, max_conn=3)

    # 增量模式：从 DB 已有数据末尾开始
    if incremental:
        coverage = _get_db_coverage(writer, symbol, timeframes)
        logger.info("Incremental mode — checking existing data...")
    else:
        coverage = {}

    total_bars = 0
    total_written = 0

    for tf in timeframes:
        # 确定起始时间
        tf_start = start
        if incremental and tf in coverage and coverage[tf]["last"]:
            tf_start = coverage[tf]["last"] + timedelta(seconds=1)
            if tf_start >= end:
                logger.info("  %s %s: already up to date (last=%s)", symbol, tf, coverage[tf]["last"])
                continue

        logger.info(
            "=== %s %s [%s ~ %s] ===",
            symbol, tf, tf_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        )

        cursor = tf_start
        tf_bars = 0
        tf_written = 0
        tf_seconds = timeframe_seconds(tf)

        while cursor < end:
            try:
                bars = client.get_ohlc_from(symbol, tf, cursor, batch_size)
            except Exception as e:
                logger.error("  Fetch failed at %s: %s — skipping batch", cursor, e)
                cursor += timedelta(seconds=tf_seconds * batch_size)
                continue

            if not bars:
                logger.info("  No more bars after %s", cursor.strftime("%Y-%m-%d %H:%M"))
                break

            # 过滤超出 end 的 bar
            bars = [b for b in bars if b.time <= end]
            if not bars:
                break

            tf_bars += len(bars)

            if not dry_run:
                rows = [
                    (
                        b.symbol,
                        b.timeframe,
                        b.open,
                        b.high,
                        b.low,
                        b.close,
                        float(b.volume or 0.0),
                        b.time.isoformat(),
                    )
                    for b in bars
                ]
                try:
                    writer.write_ohlc(rows, upsert=True)
                    tf_written += len(rows)
                except Exception as e:
                    logger.error("  Write failed (%d bars): %s", len(rows), e)

            cursor = bars[-1].time + timedelta(seconds=1)
            logger.info(
                "  %s: +%d bars → %s (total: %d)",
                tf, len(bars), bars[-1].time.strftime("%Y-%m-%d %H:%M"), tf_bars,
            )
            time.sleep(0.3)

        total_bars += tf_bars
        total_written += tf_written
        mode = "dry-run" if dry_run else "written"
        logger.info("  %s complete: %d fetched, %d %s", tf, tf_bars, tf_written, mode)

    # 回填后显示覆盖状态
    final_coverage = _get_db_coverage(writer, symbol, timeframes)
    print(_render_status(final_coverage, symbol))

    writer.close()
    client.shutdown()
    logger.info(
        "=== Done: %d bars fetched, %d written, %d timeframes ===",
        total_bars, total_written, len(timeframes),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 → TimescaleDB OHLC backfill")
    parser.add_argument("--symbol", default="XAUUSD", help="Trading symbol (default: XAUUSD)")
    parser.add_argument(
        "--tf",
        default="M5,M15,M30,H1,H4,D1",
        help="Timeframes, comma-separated (default: M5,M15,M30,H1,H4,D1)",
    )
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: 6 months ago)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--batch-size", type=int, default=5000, help="Bars per batch (default: 5000)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, don't write to DB")
    parser.add_argument("--incremental", action="store_true", help="Continue from last bar in DB")
    parser.add_argument("--status", action="store_true", help="Show current DB coverage and exit")
    args = parser.parse_args()

    timeframes = [tf.strip().upper() for tf in args.tf.split(",")]

    # --status 模式：只显示覆盖状态
    if args.status:
        from src.config.database import load_db_settings
        from src.persistence.db import TimescaleWriter

        writer = TimescaleWriter(load_db_settings(), min_conn=1, max_conn=2)
        coverage = _get_db_coverage(writer, args.symbol, timeframes)
        print(_render_status(coverage, args.symbol))
        writer.close()
        return

    # 默认日期范围
    if args.end:
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(timezone.utc)

    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    else:
        start = end - timedelta(days=180)  # 默认 6 个月

    backfill(
        symbol=args.symbol,
        timeframes=timeframes,
        start=start,
        end=end,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        incremental=args.incremental,
    )


if __name__ == "__main__":
    main()
