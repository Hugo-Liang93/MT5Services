from __future__ import annotations

from src.api import app


def test_non_health_business_routes_are_versioned_only() -> None:
    paths = {route.path for route in app.routes}

    assert "/v1/trade" in paths
    assert "/v1/decision/brief" in paths
    assert "/v1/monitoring/health" in paths
    assert "/trade" not in paths
    assert "/monitoring/health" not in paths
