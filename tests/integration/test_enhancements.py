from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_event_store() -> None:
    test_db = "test_events.db"
    event_store = None
    try:
        from src.utils.event_store import LocalEventStore

        event_store = LocalEventStore(test_db)
        test_time = datetime.now(timezone.utc)
        event_id = event_store.publish_event("XAUUSD", "M1", test_time)

        event = event_store.claim_next_event()
        assert event is not None
        assert event.event_id == event_id
        assert event_store.mark_event_completed_by_id(event.event_id)

        stats = event_store.get_stats()
        assert stats["total"] >= 1
    finally:
        if event_store is not None:
            event_store.close()
        if os.path.exists(test_db):
            os.remove(test_db)


def test_health_monitor() -> None:
    test_db = "test_health.db"
    monitor = None
    try:
        from src.monitoring import HealthMonitor

        monitor = HealthMonitor(test_db)
        monitor.record_metric(
            "test_component",
            "test_metric",
            42.0,
            {"details": "test data"},
        )
        report = monitor.generate_report(hours=1)
        metrics = monitor.get_recent_metrics("test_component", "test_metric", limit=5)

        assert report["overall_status"] in {"healthy", "warning", "critical"}
        assert len(metrics) >= 1
    finally:
        if monitor is not None:
            monitor.close()
        # Clean up all possible DB files (legacy health.db + new alerts DB + WAL files)
        for pattern_path in [test_db, "health_alerts.db"]:
            for suffix in ["", "-shm", "-wal"]:
                p = pattern_path + suffix
                if os.path.exists(p):
                    os.remove(p)


def compare_optimizations() -> None:
    comparison = {
        "event_driven": {
            "before": "in-memory queue only",
            "after": "durable sqlite event store",
        },
        "error_handling": {
            "before": "weak failure recovery",
            "after": "retryable event persistence",
        },
        "monitoring": {
            "before": "basic status checks",
            "after": "health reporting and observability endpoints",
        },
    }
    print(json.dumps(comparison, indent=2))


def check_dependencies() -> None:
    logger.info("Dependency check placeholder")


def main() -> None:
    test_event_store()
    test_health_monitor()
    compare_optimizations()
    check_dependencies()


if __name__ == "__main__":
    main()
