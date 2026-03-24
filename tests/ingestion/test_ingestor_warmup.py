"""BackgroundIngestor warmup/backfill 机制单元测试。

测试 is_backfilling 属性、回补线程生命周期、_backfill_done 标志。
使用 mock 避免真实 MT5 和 DB 依赖。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from src.ingestion.ingestor import BackgroundIngestor


# ── Minimal IngestSettings stub ───────────────────────────────


def _make_settings(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "ingest_symbols": ["XAUUSD"],
        "ingest_ohlc_timeframes": ["M5"],
        "poll_interval": 0.5,
        "ohlc_interval": 10,
        "intrabar_interval": 5,
        "max_concurrent_symbols": 0,
        "connection_timeout": 2.0,
        "symbol_error_threshold": 5,
        "symbol_cooldown_seconds": 60.0,
        "symbol_max_cooldown_seconds": 300.0,
        "backfill_batch_bars": 100,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_ingestor(
    settings: Optional[SimpleNamespace] = None,
    db_has_data: bool = False,
) -> BackgroundIngestor:
    """创建带有 mock 依赖的 BackgroundIngestor。"""
    service = MagicMock()
    storage = MagicMock()

    if db_has_data:
        # 模拟 DB 有历史数据——需要回补
        db = MagicMock()
        db.last_tick_time.return_value = datetime(2025, 1, 1, tzinfo=timezone.utc)
        db.last_ohlc_time.return_value = datetime(
            2025, 1, 1, 0, 0, tzinfo=timezone.utc
        )
        storage.db = db
    else:
        # 模拟 DB 无数据——无回补任务
        db = MagicMock()
        db.last_tick_time.return_value = None
        db.last_ohlc_time.return_value = None
        storage.db = db

    if settings is None:
        settings = _make_settings()

    ingestor = BackgroundIngestor(service, storage, settings)
    return ingestor


# ═══════════════════════════════════════════════════════════════
# is_backfilling 属性
# ═══════════════════════════════════════════════════════════════


class TestIsBackfilling:
    def test_initial_state(self) -> None:
        """创建后 _backfill_done 未 set，is_backfilling 为 True。"""
        ingestor = _make_ingestor()
        # Event 初始化后默认 not set
        assert ingestor.is_backfilling is True

    def test_after_set(self) -> None:
        """_backfill_done.set() 后 is_backfilling 为 False。"""
        ingestor = _make_ingestor()
        ingestor._backfill_done.set()
        assert ingestor.is_backfilling is False

    def test_after_clear(self) -> None:
        """_backfill_done.clear() 后 is_backfilling 恢复为 True。"""
        ingestor = _make_ingestor()
        ingestor._backfill_done.set()
        ingestor._backfill_done.clear()
        assert ingestor.is_backfilling is True


# ═══════════════════════════════════════════════════════════════
# 回补线程生命周期
# ═══════════════════════════════════════════════════════════════


class TestBackfillLifecycle:
    def test_no_backfill_tasks_sets_done_immediately(self) -> None:
        """无回补任务时，start() 直接 set _backfill_done，不启动回补线程。"""
        ingestor = _make_ingestor(db_has_data=False)

        # Mock _run 防止主轮询线程实际执行
        with patch.object(ingestor, "_run"):
            ingestor.start()

        # 无回补任务→立即完成
        assert ingestor.is_backfilling is False
        assert ingestor._backfill_thread is None

        ingestor.stop()

    def test_with_backfill_tasks_starts_thread(self) -> None:
        """有回补任务时，start() 启动回补线程。"""
        ingestor = _make_ingestor(db_has_data=True)

        # Mock 回补和主循环，防止实际执行
        with patch.object(ingestor, "_backfill_ohlc"):
            with patch.object(ingestor, "_run"):
                ingestor.start()
                # 回补线程应已启动
                assert ingestor._backfill_thread is not None
                # 等待回补线程完成（mock 下几乎立即完成）
                ingestor._backfill_thread.join(timeout=5)

        assert ingestor.is_backfilling is False
        ingestor.stop()

    def test_backfill_thread_sets_done_on_success(self) -> None:
        """回补成功完成后设置 _backfill_done。"""
        ingestor = _make_ingestor(db_has_data=True)

        with patch.object(ingestor, "_backfill_ohlc"):
            with patch.object(ingestor, "_run"):
                ingestor.start()
                ingestor._backfill_thread.join(timeout=5)

        assert ingestor._backfill_done.is_set()
        ingestor.stop()

    def test_backfill_thread_sets_done_on_exception(self) -> None:
        """回补异常后仍然设置 _backfill_done（finally 块保证）。"""
        ingestor = _make_ingestor(db_has_data=True)

        with patch.object(
            ingestor, "_backfill_ohlc", side_effect=RuntimeError("DB error")
        ):
            with patch.object(ingestor, "_run"):
                ingestor.start()
                ingestor._backfill_thread.join(timeout=5)

        # 即使异常，_backfill_done 仍应被 set
        assert ingestor._backfill_done.is_set()
        assert ingestor.is_backfilling is False
        ingestor.stop()


# ═══════════════════════════════════════════════════════════════
# _backfill_async
# ═══════════════════════════════════════════════════════════════


class TestBackfillAsync:
    def test_backfill_async_calls_backfill_ohlc(self) -> None:
        """_backfill_async 调用 _backfill_ohlc 并设置 done。"""
        ingestor = _make_ingestor()
        with patch.object(ingestor, "_backfill_ohlc") as mock_bf:
            ingestor._backfill_async()
            mock_bf.assert_called_once()
        assert ingestor._backfill_done.is_set()

    def test_backfill_async_exception_safe(self) -> None:
        """_backfill_async 异常时不传播，仍设置 done。"""
        ingestor = _make_ingestor()
        with patch.object(
            ingestor, "_backfill_ohlc", side_effect=Exception("boom")
        ):
            # 不应抛异常
            ingestor._backfill_async()
        assert ingestor._backfill_done.is_set()


# ═══════════════════════════════════════════════════════════════
# _init_backfill_progress
# ═══════════════════════════════════════════════════════════════


class TestInitBackfillProgress:
    def test_no_db_data(self) -> None:
        """DB 无数据时，_backfill_progress 为空。"""
        ingestor = _make_ingestor(db_has_data=False)
        ingestor._init_backfill_progress()
        assert len(ingestor._backfill_progress) == 0

    def test_with_db_data(self) -> None:
        """DB 有数据时，初始化 _backfill_progress 和 _backfill_cutoff。"""
        ingestor = _make_ingestor(db_has_data=True)
        ingestor._init_backfill_progress()
        # 有 1 个 symbol × 1 个 tf = 1 条进度
        assert len(ingestor._backfill_progress) == 1
        assert len(ingestor._backfill_cutoff) == 1

    def test_db_exception_handled(self) -> None:
        """DB 异常时不崩溃，_backfill_progress 为空。"""
        ingestor = _make_ingestor()
        # 让 storage.db 抛异常
        type(ingestor.storage).db = PropertyMock(side_effect=RuntimeError("no db"))
        ingestor._init_backfill_progress()
        assert len(ingestor._backfill_progress) == 0


# ═══════════════════════════════════════════════════════════════
# stop() 关闭
# ═══════════════════════════════════════════════════════════════


class TestStop:
    def test_stop_joins_threads(self) -> None:
        """stop() 正确 join 主线程和回补线程。"""
        ingestor = _make_ingestor(db_has_data=True)

        with patch.object(ingestor, "_backfill_ohlc"):
            with patch.object(ingestor, "_run"):
                ingestor.start()
                # 等待回补完成
                if ingestor._backfill_thread:
                    ingestor._backfill_thread.join(timeout=5)

        ingestor.stop()
        assert ingestor._stop.is_set()
