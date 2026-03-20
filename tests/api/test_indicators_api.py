from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_market_service, get_unified_indicator_manager
from src.api.error_codes import AIErrorCode
from src.api.indicators import IndicatorRequest, compute_indicators, router


class _IndicatorManager:
    def list_indicators(self):
        return [{"name": "ema20"}, {"name": "atr14"}]

    def get_performance_stats(self):
        return {"events": 1}

    def get_dependency_graph(self, format):
        return f"graph:{format}"

    def get_all_indicators(self, symbol, timeframe):
        return {"unexpected": {"value": 1}}

    def compute(self, symbol, timeframe, indicator_names):
        return {
            "ema20": {"ema": 3001.0},
            "atr14": None,
        }


class _MarketService:
    def list_symbols(self):
        return ["XAUUSD"]


def test_indicator_static_routes_are_not_shadowed_by_dynamic_routes() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_unified_indicator_manager] = lambda: _IndicatorManager()
    app.dependency_overrides[get_market_service] = lambda: _MarketService()
    client = TestClient(app)

    perf = client.get("/indicators/performance/stats")
    graph = client.get("/indicators/dependency/graph")

    assert perf.status_code == 200
    assert perf.json()["success"] is True
    assert perf.json()["data"]["events"] == 1
    assert graph.status_code == 200
    assert graph.json()["success"] is True
    assert graph.json()["data"]["graph"] == "graph:mermaid"


def test_compute_indicators_rejects_unsupported_indicator_names() -> None:
    response = asyncio.run(
        compute_indicators(
            IndicatorRequest(symbol="XAUUSD", timeframe="M1", indicators=["ema20", "close"]),
            manager=_IndicatorManager(),
            market_service=_MarketService(),
        )
    )

    assert response.success is False
    assert response.error["code"] == AIErrorCode.INVALID_INDICATOR_PARAMS.value.lower()
    assert response.error["details"]["invalid_indicators"] == ["close"]


def test_compute_indicators_filters_none_results_for_response_validation() -> None:
    response = asyncio.run(
        compute_indicators(
            IndicatorRequest(symbol="XAUUSD", timeframe="M1", indicators=["ema20", "atr14"]),
            manager=_IndicatorManager(),
            market_service=_MarketService(),
        )
    )

    assert response.success is True
    assert response.data == {"ema20": {"ema": 3001.0}}
    assert response.metadata["indicators_computed"] == 1
