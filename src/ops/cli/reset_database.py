"""数据库完全重置脚本。

DROP 所有表并重建 schema。仅在开发/测试环境使用。

用法：
    python -m src.ops.cli.reset_database
    python -m src.ops.cli.reset_database --yes   # 跳过确认提示
"""

from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import logging
import argparse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    from src.config.instance_context import set_current_environment

    parser = argparse.ArgumentParser(description="Reset database schema for a target environment")
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="显式指定要重置的环境数据库",
    )
    parser.add_argument("--yes", action="store_true", help="跳过确认提示")
    args = parser.parse_args()
    set_current_environment(args.environment)
    skip_confirm = args.yes

    if not skip_confirm:
        answer = input(
            "\n⚠️  此操作将 DROP 所有表并重建 schema。\n"
            "    所有现有数据将永久丢失。\n\n"
            "    确认执行？输入 'yes' 继续: "
        )
        if answer.strip().lower() != "yes":
            print("已取消。")
            return

    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    settings = load_db_settings()
    logger.info("连接数据库: %s@%s:%s/%s", settings.pg_user, settings.pg_host, settings.pg_port, settings.pg_database)

    writer = TimescaleWriter(settings, min_conn=1, max_conn=2)

    # Phase 1: DROP all tables
    logger.info("Phase 1: DROP 所有表...")
    with writer.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # 获取所有用户表
            cur.execute("""
                SELECT tablename FROM pg_tables
                WHERE schemaname = %s
                ORDER BY tablename
            """, (settings.pg_schema if hasattr(settings, 'pg_schema') else 'public',))
            tables = [row[0] for row in cur.fetchall()]

            if tables:
                logger.info("  发现 %d 张表: %s", len(tables), ", ".join(tables))
                for table in tables:
                    cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                    logger.info("  DROP TABLE %s", table)
            else:
                logger.info("  无表需要删除")

    # Phase 2: 确保 TimescaleDB 扩展存在
    logger.info("Phase 2: 确保 TimescaleDB 扩展...")
    with writer.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    # Phase 3: 重建所有表
    logger.info("Phase 3: 重建 schema...")
    writer.init_schema()

    # Phase 4: 验证
    logger.info("Phase 4: 验证...")
    with writer.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tablename FROM pg_tables
                WHERE schemaname = %s
                ORDER BY tablename
            """, (settings.pg_schema if hasattr(settings, 'pg_schema') else 'public',))
            new_tables = [row[0] for row in cur.fetchall()]

            # 检查 hypertable
            cur.execute("""
                SELECT hypertable_name FROM timescaledb_information.hypertables
                ORDER BY hypertable_name
            """)
            hypertables = [row[0] for row in cur.fetchall()]

    logger.info("  创建了 %d 张表:", len(new_tables))
    for t in new_tables:
        ht_marker = " [hypertable]" if t in hypertables else ""
        logger.info("    %s%s", t, ht_marker)

    logger.info("\n数据库重置完成。共 %d 张表，其中 %d 张 hypertable。",
                len(new_tables), len(hypertables))

    writer.close()


if __name__ == "__main__":
    main()

