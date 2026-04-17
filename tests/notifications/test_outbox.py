"""Tests for the SQLite-backed OutboxStore."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.notifications.persistence.outbox import (
    OutboxStatus,
    OutboxStore,
    compute_next_retry_at,
)


def _make_store(tmp_path: Path) -> OutboxStore:
    return OutboxStore(tmp_path / "outbox.db")


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestEnqueue:
    def test_basic_enqueue(self, tmp_path: Path):
        store = _make_store(tmp_path)
        row_id = store.enqueue(
            event_id="evt1",
            event_type="execution_failed",
            severity="critical",
            chat_id="123",
            rendered_text="hello",
            dedup_key="execution_failed:XAUUSD",
            now=_now(),
        )
        assert row_id > 0
        due = store.fetch_due(now=_now())
        assert len(due) == 1
        assert due[0].event_id == "evt1"
        assert due[0].status is OutboxStatus.PENDING
        assert due[0].attempt_count == 0
        store.close()

    def test_duplicate_event_id_rejected(self, tmp_path: Path):
        store = _make_store(tmp_path)
        store.enqueue(
            event_id="evt1",
            event_type="t",
            severity="critical",
            chat_id="1",
            rendered_text="x",
            dedup_key="k",
            now=_now(),
        )
        with pytest.raises(ValueError, match="already in outbox"):
            store.enqueue(
                event_id="evt1",
                event_type="t",
                severity="critical",
                chat_id="1",
                rendered_text="x",
                dedup_key="k",
                now=_now(),
            )
        store.close()


class TestFetchDue:
    def test_fetch_respects_retry_time(self, tmp_path: Path):
        store = _make_store(tmp_path)
        now = _now()
        future = now + timedelta(seconds=60)
        row_id = store.enqueue(
            event_id="evt1",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k",
            now=future,  # scheduled in the future
        )
        # now < next_retry_at → no due rows
        assert store.fetch_due(now=now) == []
        # at future time → 1 row
        assert len(store.fetch_due(now=future)) == 1
        store.close()

    def test_fetch_skips_sent(self, tmp_path: Path):
        store = _make_store(tmp_path)
        row_id = store.enqueue(
            event_id="evt1",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k",
            now=_now(),
        )
        store.mark_sent(row_id)
        assert store.fetch_due(now=_now()) == []
        store.close()

    def test_fetch_skips_dlq(self, tmp_path: Path):
        store = _make_store(tmp_path)
        row_id = store.enqueue(
            event_id="evt1",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k",
            now=_now(),
        )
        store.mark_dlq(row_id, error="too many attempts")
        assert store.fetch_due(now=_now()) == []
        store.close()

    def test_fetch_ordering_by_id(self, tmp_path: Path):
        store = _make_store(tmp_path)
        for i in range(3):
            store.enqueue(
                event_id=f"e{i}",
                event_type="t",
                severity="c",
                chat_id="1",
                rendered_text="x",
                dedup_key=f"k{i}",
                now=_now(),
            )
        due = store.fetch_due(now=_now(), limit=10)
        assert [e.event_id for e in due] == ["e0", "e1", "e2"]
        store.close()

    def test_fetch_limit_applied(self, tmp_path: Path):
        store = _make_store(tmp_path)
        for i in range(5):
            store.enqueue(
                event_id=f"e{i}",
                event_type="t",
                severity="c",
                chat_id="1",
                rendered_text="x",
                dedup_key=f"k{i}",
                now=_now(),
            )
        assert len(store.fetch_due(now=_now(), limit=2)) == 2
        store.close()


class TestMarkFailed:
    def test_failed_remains_pending_with_incremented_attempt(self, tmp_path: Path):
        store = _make_store(tmp_path)
        row_id = store.enqueue(
            event_id="evt1",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k",
            now=_now(),
        )
        retry_at = _now() + timedelta(seconds=30)
        store.mark_failed(row_id, error="http 500", next_retry_at=retry_at)
        due_later = store.fetch_due(now=retry_at)
        assert len(due_later) == 1
        entry = due_later[0]
        assert entry.attempt_count == 1
        assert entry.last_error == "http 500"
        assert entry.status is OutboxStatus.PENDING
        store.close()

    def test_repeated_failures_accumulate_attempts(self, tmp_path: Path):
        store = _make_store(tmp_path)
        row_id = store.enqueue(
            event_id="evt1",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k",
            now=_now(),
        )
        for i in range(3):
            store.mark_failed(row_id, error=f"err{i}", next_retry_at=_now())
        entry = store.fetch_due(now=_now())[0]
        assert entry.attempt_count == 3


class TestDlq:
    def test_mark_dlq_terminal(self, tmp_path: Path):
        store = _make_store(tmp_path)
        row_id = store.enqueue(
            event_id="evt1",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k",
            now=_now(),
        )
        store.mark_dlq(row_id, error="gave up")
        assert store.fetch_due(now=_now()) == []
        entries = store.dlq_entries()
        assert len(entries) == 1
        assert entries[0].status is OutboxStatus.DLQ
        assert entries[0].last_error == "gave up"
        store.close()


class TestCounts:
    def test_count_by_status(self, tmp_path: Path):
        store = _make_store(tmp_path)
        id1 = store.enqueue(
            event_id="a",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k1",
            now=_now(),
        )
        id2 = store.enqueue(
            event_id="b",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k2",
            now=_now(),
        )
        id3 = store.enqueue(
            event_id="c",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k3",
            now=_now(),
        )
        store.mark_sent(id1)
        store.mark_dlq(id2, error="x")
        counts = store.count_by_status()
        assert counts.get("pending", 0) == 1
        assert counts.get("sent", 0) == 1
        assert counts.get("dlq", 0) == 1


class TestPurge:
    def test_purge_sent_older_than(self, tmp_path: Path):
        store = _make_store(tmp_path)
        old = _now() - timedelta(days=10)
        for i in range(3):
            row_id = store.enqueue(
                event_id=f"e{i}",
                event_type="t",
                severity="c",
                chat_id="1",
                rendered_text="x",
                dedup_key=f"k{i}",
                now=old,
            )
            store.mark_sent(row_id)
        # One recent sent entry
        recent_id = store.enqueue(
            event_id="recent",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="x",
            dedup_key="k_recent",
            now=_now(),
        )
        store.mark_sent(recent_id)
        cutoff = _now() - timedelta(days=7)
        deleted = store.purge_sent_older_than(cutoff)
        assert deleted == 3
        counts = store.count_by_status()
        assert counts.get("sent", 0) == 1
        store.close()


class TestRecovery:
    def test_pending_survives_restart(self, tmp_path: Path):
        db_path = tmp_path / "outbox.db"
        first = OutboxStore(db_path)
        first.enqueue(
            event_id="evt1",
            event_type="t",
            severity="c",
            chat_id="1",
            rendered_text="survives reboot",
            dedup_key="k",
            now=_now(),
        )
        first.close()
        # Simulate restart
        second = OutboxStore(db_path)
        due = second.fetch_due(now=_now())
        assert len(due) == 1
        assert due[0].rendered_text == "survives reboot"
        second.close()


class TestBackoffHelper:
    def test_compute_next_retry_step_by_step(self):
        now = _now()
        backoff = [5.0, 15.0, 45.0]
        assert compute_next_retry_at(
            attempt_count=0, backoff_seconds=backoff, now=now
        ) == now + timedelta(seconds=5)
        assert compute_next_retry_at(
            attempt_count=1, backoff_seconds=backoff, now=now
        ) == now + timedelta(seconds=15)
        assert compute_next_retry_at(
            attempt_count=2, backoff_seconds=backoff, now=now
        ) == now + timedelta(seconds=45)

    def test_compute_next_retry_clamps_past_end(self):
        now = _now()
        backoff = [5.0, 15.0]
        # attempt_count=99 should clamp to last entry (15s)
        assert compute_next_retry_at(
            attempt_count=99, backoff_seconds=backoff, now=now
        ) == now + timedelta(seconds=15)

    def test_empty_backoff_raises(self):
        with pytest.raises(ValueError):
            compute_next_retry_at(attempt_count=0, backoff_seconds=[], now=_now())
