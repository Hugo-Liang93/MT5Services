from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.readmodels.intel import IntelReadModel


class _FakeSignalModule:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        account_bindings: dict[str, list[str]] | None = None,
        raise_on_bindings: bool = False,
    ) -> None:
        self._rows = rows
        self.page_kwargs: dict[str, Any] | None = None
        # strategy → [account_alias] 反向索引（P10.2 统一路径）
        self._reverse_bindings: dict[str, list[str]] = {}
        self._raise_on_bindings = raise_on_bindings
        for account_alias, strategy_list in (account_bindings or {}).items():
            for strategy in strategy_list:
                self._reverse_bindings.setdefault(strategy, []).append(account_alias)

    def recent_signal_page(self, **kwargs: Any) -> dict[str, Any]:
        self.page_kwargs = kwargs
        return {
            "items": self._rows,
            "total": len(self._rows),
            "page": kwargs.get("page") or 1,
            "page_size": kwargs.get("page_size") or 25,
        }

    def strategy_account_bindings(self) -> dict[str, list[str]]:
        if self._raise_on_bindings:
            raise RuntimeError("bindings unavailable")
        return {k: list(v) for k, v in self._reverse_bindings.items()}


def _signal_row(
    *,
    signal_id: str,
    strategy: str,
    symbol: str = "XAUUSD",
    confidence: float = 0.7,
    actionability: str = "actionable",
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "trace_id": f"trace_{signal_id}",
        "symbol": symbol,
        "timeframe": "H1",
        "strategy": strategy,
        "direction": "buy",
        "confidence": confidence,
        "actionability": actionability,
        "guard_reason_code": None,
        "priority": confidence,
        "rank_source": "native",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def test_intel_queue_enriches_each_signal_with_account_candidates() -> None:
    signal_module = _FakeSignalModule(
        [_signal_row(signal_id="sig_1", strategy="breakout_follow")],
        account_bindings={
            "live_main": ["breakout_follow", "trend_continuation"],
            "live_exec_a": ["breakout_follow"],
            "demo_main": ["range_reversion"],
        },
    )
    model = IntelReadModel(signal_module=signal_module)

    payload = model.build_action_queue(symbol="XAUUSD")

    assert signal_module.page_kwargs is not None
    assert signal_module.page_kwargs["actionability"] == "actionable"
    assert signal_module.page_kwargs["sort"] == "priority_desc"

    entries = payload["entries"]
    assert len(entries) == 1
    entry = entries[0]
    aliases = [c["account_alias"] for c in entry["account_candidates"]]
    assert "live_main" in aliases
    assert "live_exec_a" in aliases
    assert "demo_main" not in aliases
    assert entry["recommended_action"] == "execute_from_signal"


def test_intel_queue_recommends_review_when_no_account_binding() -> None:
    signal_module = _FakeSignalModule(
        [_signal_row(signal_id="sig_2", strategy="sweep_reversal")],
        account_bindings={"live_main": ["breakout_follow"]},
    )
    model = IntelReadModel(signal_module=signal_module)

    payload = model.build_action_queue()

    entry = payload["entries"][0]
    assert entry["account_candidates"] == []
    assert entry["recommended_action"] == "review_strategy_deployment"


def test_intel_queue_freshness_reports_observed_at_and_state() -> None:
    signal_module = _FakeSignalModule(
        [_signal_row(signal_id="sig_3", strategy="breakout_follow")],
        account_bindings={"live_main": ["breakout_follow"]},
    )
    model = IntelReadModel(signal_module=signal_module)

    payload = model.build_action_queue()

    freshness = payload["freshness"]
    assert freshness["source_kind"] == "native"
    assert freshness["freshness_state"] in {"fresh", "warn", "stale"}
    assert "observed_at" in freshness
    assert "data_updated_at" in freshness


def test_intel_queue_handles_bindings_error_gracefully() -> None:
    signal_module = _FakeSignalModule(
        [_signal_row(signal_id="sig_4", strategy="breakout_follow")],
        raise_on_bindings=True,
    )
    model = IntelReadModel(signal_module=signal_module)

    payload = model.build_action_queue()
    entry = payload["entries"][0]
    assert entry["account_candidates"] == []
    assert entry["recommended_action"] == "review_strategy_deployment"
