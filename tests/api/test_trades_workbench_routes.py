from __future__ import annotations

from typing import Any

from src.api.trade import trade_detail, trades_workbench


class _StubTradesWorkbenchReadModel:
    def __init__(self) -> None:
        self.workbench_kwargs: dict[str, Any] = {}
        self.detail_trade_id: str | None = None

    def build_workbench(self, **kwargs: Any) -> dict[str, Any]:
        self.workbench_kwargs = kwargs
        return {
            "account_alias": "live_main",
            "observed_at": "2026-04-20T10:00:00+00:00",
            "records": [
                {
                    "trade_id": "sig_42",
                    "signal_id": "sig_42",
                    "recorded_at": "2026-04-20T09:00:00+00:00",
                    "symbol": kwargs.get("symbol") or "XAUUSD",
                    "strategy": kwargs.get("strategy") or "breakout_follow",
                    "won": True,
                }
            ],
            "summary": {"total_count": 1, "win_rate": 1.0},
            "pagination": {
                "page": kwargs.get("page") or 1,
                "page_size": kwargs.get("page_size") or 50,
                "total": 1,
                "sort": kwargs.get("sort") or "recorded_at_desc",
            },
            "freshness": {"source_kind": "native"},
        }

    def build_trade_detail(self, *, trade_id: str) -> dict[str, Any] | None:
        self.detail_trade_id = trade_id
        if trade_id == "missing":
            return None
        return {
            "trade_id": trade_id,
            "signal_id": trade_id,
            "trace_id": "trace_42",
            "observed_at": "2026-04-20T10:00:00+00:00",
            "plan_vs_live": {"plan": {}, "live": {}},
            "lifecycle": {"timeline": []},
            "risk_review": {"last_decision": "allow"},
            "receipts": {"count": 0, "entries": []},
            "evidence": {"strategy": "breakout_follow"},
            "linked_account_state": {"count": 0},
            "freshness": {"source_kind": "native"},
        }


def test_trades_workbench_route_passes_filters_and_pagination() -> None:
    read_model = _StubTradesWorkbenchReadModel()

    response = trades_workbench(
        symbol="XAUUSD",
        strategy="breakout_follow",
        page=2,
        page_size=25,
        sort="recorded_at_asc",
        read_model=read_model,
    )

    assert response.success is True
    assert response.data["account_alias"] == "live_main"
    assert read_model.workbench_kwargs["symbol"] == "XAUUSD"
    assert read_model.workbench_kwargs["strategy"] == "breakout_follow"
    assert read_model.workbench_kwargs["page"] == 2
    assert read_model.workbench_kwargs["page_size"] == 25
    assert response.metadata["sort"] == "recorded_at_asc"
    assert response.metadata["total"] == 1


def test_trade_detail_route_returns_six_dimensions_for_known_trade() -> None:
    read_model = _StubTradesWorkbenchReadModel()

    response = trade_detail(trade_id="sig_42", read_model=read_model)

    assert response.success is True
    assert response.data["trade_id"] == "sig_42"
    assert response.data["risk_review"]["last_decision"] == "allow"
    assert response.metadata["trace_id"] == "trace_42"
    assert read_model.detail_trade_id == "sig_42"


def test_trade_detail_route_returns_not_found() -> None:
    read_model = _StubTradesWorkbenchReadModel()

    response = trade_detail(trade_id="missing", read_model=read_model)

    assert response.success is False
    assert response.error["code"] == "not_found"


def test_trade_detail_route_rejects_blank_trade_id() -> None:
    read_model = _StubTradesWorkbenchReadModel()

    response = trade_detail(trade_id="  ", read_model=read_model)

    assert response.success is False
    assert response.error["code"] == "validation_error"
    assert read_model.detail_trade_id is None
