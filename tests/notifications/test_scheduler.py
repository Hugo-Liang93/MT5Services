"""Tests for NotificationScheduler (DailyAt + Interval jobs + lifecycle)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.notifications.scheduler import DailyAtJob, IntervalJob, NotificationScheduler


class TestDailyAtJob:
    def test_not_due_before_target(self):
        job = DailyAtJob(name="t", job=lambda: None, hour_utc=21, minute_utc=0)
        now = datetime(2026, 1, 1, 20, 30, tzinfo=timezone.utc)
        assert job.is_due(now=now) is False

    def test_due_at_target(self):
        job = DailyAtJob(name="t", job=lambda: None, hour_utc=21, minute_utc=0)
        now = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc)
        assert job.is_due(now=now) is True

    def test_due_after_target(self):
        job = DailyAtJob(name="t", job=lambda: None, hour_utc=21, minute_utc=0)
        now = datetime(2026, 1, 1, 21, 30, tzinfo=timezone.utc)
        assert job.is_due(now=now) is True

    def test_not_due_after_firing_same_day(self):
        job = DailyAtJob(name="t", job=lambda: None, hour_utc=21, minute_utc=0)
        now = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc)
        assert job.is_due(now=now) is True
        job.mark_fired(now=now)
        later = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
        assert job.is_due(now=later) is False

    def test_due_again_next_day(self):
        job = DailyAtJob(name="t", job=lambda: None, hour_utc=21, minute_utc=0)
        first = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc)
        job.mark_fired(now=first)
        next_day = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
        assert job.is_due(now=next_day) is True


class TestIntervalJob:
    def test_first_tick_does_not_fire_immediately(self):
        job = IntervalJob(name="i", job=lambda: None, interval_seconds=60)
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # First observation schedules next_fire_at, doesn't fire yet.
        assert job.is_due(now=now) is False

    def test_due_after_interval(self):
        job = IntervalJob(name="i", job=lambda: None, interval_seconds=60)
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        job.is_due(now=base)  # primes the schedule
        assert job.is_due(now=base + timedelta(seconds=59)) is False
        assert job.is_due(now=base + timedelta(seconds=60)) is True

    def test_mark_fired_schedules_forward(self):
        job = IntervalJob(name="i", job=lambda: None, interval_seconds=60)
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        job.is_due(now=base)
        fire_at = base + timedelta(seconds=90)  # we're late by 30s
        job.mark_fired(now=fire_at)
        # Next fire should be 60s AFTER this fire (not 60s after original schedule
        # — avoids catch-up bursts).
        assert job.is_due(now=fire_at + timedelta(seconds=50)) is False
        assert job.is_due(now=fire_at + timedelta(seconds=60)) is True


class TestSchedulerTick:
    def test_tick_fires_due_jobs(self):
        scheduler = NotificationScheduler()
        counter = {"n": 0}
        scheduler.add_daily(
            name="daily",
            hour_utc=0,
            minute_utc=0,
            job=lambda: counter.__setitem__("n", counter["n"] + 1),
        )
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        fired = scheduler.tick_once(now=now)
        assert fired == 1
        assert counter["n"] == 1
        # Second tick same day should NOT refire
        fired = scheduler.tick_once(now=now)
        assert fired == 0

    def test_job_exception_isolated(self):
        scheduler = NotificationScheduler()
        counter = {"a": 0, "b": 0}

        def good_a():
            counter["a"] += 1

        def bad():
            raise RuntimeError("boom")

        def good_b():
            counter["b"] += 1

        scheduler.add_daily(name="a", hour_utc=0, minute_utc=0, job=good_a)
        scheduler.add_daily(name="bad", hour_utc=0, minute_utc=0, job=bad)
        scheduler.add_daily(name="b", hour_utc=0, minute_utc=0, job=good_b)
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        # Should not raise; both good jobs still fire.
        scheduler.tick_once(now=now)
        assert counter == {"a": 1, "b": 1}

    def test_interval_job_via_tick(self):
        scheduler = NotificationScheduler()
        counter = {"n": 0}
        scheduler.add_interval(
            name="poll",
            interval_seconds=10,
            job=lambda: counter.__setitem__("n", counter["n"] + 1),
        )
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        # First tick: primes schedule, doesn't fire.
        scheduler.tick_once(now=base)
        assert counter["n"] == 0
        # 10s later: fires.
        scheduler.tick_once(now=base + timedelta(seconds=10))
        assert counter["n"] == 1
        # 19s: not due (next at 20s).
        scheduler.tick_once(now=base + timedelta(seconds=19))
        assert counter["n"] == 1
        # 20s: due again.
        scheduler.tick_once(now=base + timedelta(seconds=20))
        assert counter["n"] == 2

    def test_invalid_hour_rejected(self):
        scheduler = NotificationScheduler()
        with pytest.raises(ValueError):
            scheduler.add_daily(name="x", hour_utc=25, minute_utc=0, job=lambda: None)

    def test_invalid_interval_rejected(self):
        scheduler = NotificationScheduler()
        with pytest.raises(ValueError):
            scheduler.add_interval(name="x", interval_seconds=0, job=lambda: None)


class TestSchedulerLifecycle:
    def test_start_stop(self):
        scheduler = NotificationScheduler(tick_seconds=0.05)
        scheduler.start()
        assert scheduler.is_running() is True
        scheduler.stop(timeout=2.0)
        assert scheduler.is_running() is False

    def test_double_start_no_second_thread(self):
        scheduler = NotificationScheduler(tick_seconds=0.05)
        scheduler.start()
        first = scheduler._thread
        scheduler.start()
        assert scheduler._thread is first
        scheduler.stop(timeout=2.0)

    def test_stop_without_start(self):
        scheduler = NotificationScheduler()
        # Must not raise.
        scheduler.stop(timeout=1.0)
