#!/usr/bin/env python3
"""历史 OHLC 数据回补脚本。

从 MT5 终端批量下载指定品种和时间框架的历史 K 线数据，写入 TimescaleDB。
用于回补回测所需的历史数据（如 6 个月以上的 M5/M15/H1 数据）。

用法：
    python scripts/backfill_ohlc.py --symbol XAUUSD --timeframes M5,M15,M30,H1 \
        --start 2025-09-01 --end 2026-03-28 --batch-size 5000

注意：
    - 需要 MT5 终端运行中且已登录
    - 数据写入 ohlc_bars 表（upsert 模式，不会重复插入）
    - 每个 TF 分批下载，每批 batch_size 根 K 线
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 确保项目根目录在 sys.path 中
sys.path.insert(0, ".")


def backfill(
    symbol: str,
    timeframes: List[str],
    start: datetime,
    end: datetime,
    batch_size: int = 5000,
    dry_run: bool = False,
) -> None:
    from src.clients.mt5_market import MT5MarketClient
    from src.config.mt5 import load_mt5_settings
    from src.persistence.db import TimescaleWriter
    from src.config.database import load_db_settings

    # 初始化 MT5 客户端
    mt5_settings = load_mt5_settings()
    client = MT5MarketClient(mt5_settings)

    # 初始化 DB 写入器
    db_settings = load_db_settings()
    writer = TimescaleWriter(db_settings, min_conn=1, max_conn=3)

    total_bars = 0
    total_written = 0

    for tf in timeframes:
        logger.info("=== Backfilling %s %s [%s ~ %s] ===", symbol, tf, start.date(), end.date())

        cursor = start
        tf_bars = 0
        tf_written = 0

        while cursor < end:
            try:
                bars = client.get_ohlc_from(symbol, tf, cursor, batch_size)
            except Exception as e:
                logger.error("Failed to fetch %s %s from %s: %s", symbol, tf, cursor, e)
                # 跳过这个批次，前进一个 batch 的时间
                from src.utils.common import timeframe_seconds
                cursor += timedelta(seconds=timeframe_seconds(tf) * batch_size)
                continue

            if not bars:
                logger.info("  No more bars after %s", cursor)
                break

            # 过滤超出 end 的 bar
            bars = [b for b in bars if b.time <= end]
            if not bars:
                break

            tf_bars += len(bars)

            if not dry_run:
                # 写入 DB（upsert）
                rows = [
                    (b.symbol, b.timeframe, b.time, b.open, b.high, b.low, b.close, float(b.volume or 0.0))
                    for b in bars
                ]
                try:
                    writer.write_ohlc(rows, upsert=True)
                    tf_written += len(rows)
                except Exception as e:
                    logger.error("Failed to write %d bars: %s", len(rows), e)

            # 前进游标到最后一根 bar 之后
            cursor = bars[-1].time + timedelta(seconds=1)

            logger.info(
                "  %s %s: fetched %d bars up to %s (total: %d)",
                symbol, tf, len(bars), bars[-1].time.strftime("%Y-%m-%d %H:%M"),
                tf_bars,
            )

            # 防止 MT5 API 过载
            time.sleep(0.5)

        total_bars += tf_bars
        total_written += tf_written
        logger.info(
            "  %s %s complete: %d bars fetched, %d written",
            symbol, tf, tf_bars, tf_written,
        )

    writer.close()
    logger.info(
        "=== Backfill complete: %d bars fetched, %d written across %d timeframes ===",
        total_bars, total_written, len(timeframes),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="历史 OHLC 数据回补")
    parser.add_argument("--symbol", default="XAUUSD", help="交易品种")
    parser.add_argument(
        "--timeframes", default="M5,M15,M30,H1,H4,D1",
        help="时间框架列表（逗号分隔）",
    )
    parser.add_argument("--start", required=True, help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--batch-size", type=int, default=5000, help="每批下载 K 线数")
    parser.add_argument("--dry-run", action="store_true", help="只下载不写入 DB")

    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    timeframes = [tf.strip().upper() for tf in args.timeframes.split(",")]

    backfill(
        symbol=args.symbol,
        timeframes=timeframes,
        start=start,
        end=end,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
