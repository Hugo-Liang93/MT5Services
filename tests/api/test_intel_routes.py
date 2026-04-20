from __future__ import annotations

from typing import Any

from src.api.intel import intel_action_queue


class _StubIntelReadModel:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None

    def build_action_queue(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {
            "observed_at": "2026-04-20T10:00:00+00:00",
            "entries": [
                {
                    "id": "sig_1",
                    "signal_id": "sig_1",
                    "trace_id": "trace_1",
                    "symbol": kwargs.get("symbol") or "XAUUSD",
                    "timeframe": kwargs.get("timeframe") or "H1",
                    "strategy": kwargs.get("strategy") or "breakout_follow",
                    "direction": "buy",
                    "actionability": "actionable",
                    "guard_reason_code": None,
                    "priority": 0.72,
                    "rank_source": "native",
                    "account_candidates": [{"account_alias": "live_main"}],
                    "recommended_action": "execute_from_signal",
                }
            ],
            "pagination": {
                "page": kwargs.get("page") or 1,
                "page_size": kwargs.get("page_size") or 25,
                "total": 1,
            },
            "freshness": {"source_kind": "native", "freshness_state": "fresh"},
            "filters": kwargs,
        }


def test_intel_action_queue_route_passes_filters() -> None:
    read_model = _StubIntelReadModel()

    response = intel_action_queue(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="breakout_follow",
        page=2,
        page_size=10,
        read_model=read_model,
    )

    assert response.success is True
    assert read_model.last_kwargs == {
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "strategy": "breakout_follow",
        "page": 2,
        "page_size": 10,
    }
    entry = response.data["entries"][0]
    assert entry["recommended_action"] == "execute_from_signal"
    assert response.metadata["entry_count"] == 1


def test_intel_action_queue_route_defaults_accept_empty_filters() -> None:
    read_model = _StubIntelReadModel()

    response = intel_action_queue(read_model=read_model)

    assert response.success is True
    assert response.data["entries"][0]["signal_id"] == "sig_1"
