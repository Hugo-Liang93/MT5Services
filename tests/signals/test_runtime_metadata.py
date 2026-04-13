from __future__ import annotations

from datetime import datetime, timezone

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.orchestration.runtime_metadata import build_snapshot_metadata


class _MarketService:
    @staticmethod
    def get_current_spread(symbol: str) -> float:
        assert symbol == "XAUUSD"
        return 12.0

    @staticmethod
    def get_symbol_point(symbol: str) -> float:
        assert symbol == "XAUUSD"
        return 0.01

    @staticmethod
    def get_intrabar_metadata(symbol: str, timeframe: str, *, bar_time: datetime):
        assert symbol == "XAUUSD"
        assert timeframe == "M5"
        assert bar_time == datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        return {
            "trigger_tf": "M1",
            "synthesized_at": "2026-04-13T09:00:05+00:00",
            "stale_threshold_seconds": 180.0,
            "expected_interval_seconds": 60.0,
            "last_child_bar_time": "2026-04-13T09:00:00+00:00",
            "child_bar_count": 4,
            "count": 7,
            "bar_time": "2026-04-13T09:00:00+00:00",
        }


class _SnapshotSource:
    market_service = _MarketService()


def test_build_snapshot_metadata_includes_intrabar_synthesis_context() -> None:
    metadata = build_snapshot_metadata(
        scope="intrabar",
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
        snapshot_source=_SnapshotSource(),
        trace_id="trace_intrabar_meta_1",
    )

    assert metadata[MK.SIGNAL_TRACE_ID] == "trace_intrabar_meta_1"
    assert metadata[MK.INTRABAR_TRIGGER_TF] == "M1"
    assert metadata[MK.INTRABAR_SYNTHESIS]["stale_threshold_seconds"] == 180.0
    assert metadata[MK.INTRABAR_SYNTHESIS]["child_bar_count"] == 4
