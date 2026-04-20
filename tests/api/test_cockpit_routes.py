from __future__ import annotations

from typing import Any

from src.api.cockpit import cockpit_overview


class _StubCockpitReadModel:
    def __init__(self) -> None:
        self.last_include: list[str] | None = None

    def build_overview(self, *, include: list[str] | None = None) -> dict[str, Any]:
        self.last_include = include
        blocks = include or [
            "decision",
            "triage_queue",
            "market_guard",
            "data_health",
            "exposure_map",
            "opportunity_queue",
            "safe_actions",
        ]
        payload: dict[str, Any] = {
            "observed_at": "2026-04-20T10:00:00+00:00",
            "source": {"kind": "live", "fallback_applied": False, "fallback_reason": None},
            "freshness_hints": {},
            "accounts": [
                {
                    "account_alias": "live_main",
                    "account_key": "live:1",
                    "updated_at": "2026-04-20T10:00:00+00:00",
                }
            ],
        }
        for block in blocks:
            payload[block] = {"source_kind": "native"}
        return payload


def test_cockpit_overview_returns_all_blocks_by_default() -> None:
    read_model = _StubCockpitReadModel()

    response = cockpit_overview(include=None, read_model=read_model)

    assert response.success is True
    assert read_model.last_include is None
    # 7 个业务块 + observed_at/source/freshness_hints/accounts
    blocks = {"decision", "triage_queue", "market_guard", "data_health",
              "exposure_map", "opportunity_queue", "safe_actions"}
    assert blocks.issubset(response.data.keys())
    assert response.metadata["source_kind"] == "live"
    assert response.metadata["account_count"] == 1


def test_cockpit_overview_parses_include_csv() -> None:
    read_model = _StubCockpitReadModel()

    response = cockpit_overview(
        include="decision, triage_queue , market_guard",
        read_model=read_model,
    )

    assert response.success is True
    assert read_model.last_include == ["decision", "triage_queue", "market_guard"]
    assert "decision" in response.data
    assert "exposure_map" not in response.data


def test_cockpit_overview_ignores_blank_include() -> None:
    read_model = _StubCockpitReadModel()

    response = cockpit_overview(include="   ", read_model=read_model)

    assert response.success is True
    assert read_model.last_include is None
