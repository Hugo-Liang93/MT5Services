from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.readmodels.cockpit import (
    COCKPIT_BLOCKS,
    TRIAGE_LAYER_CRITICAL,
    TRIAGE_LAYER_HEALTHY,
    TRIAGE_LAYER_WARN,
    CockpitReadModel,
)


def _now_iso(offset_seconds: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


class _FakeTradingStateRepo:
    def __init__(
        self,
        *,
        risk_states: list[dict[str, Any]],
        exposure_rows: list[dict[str, Any]],
        pending_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._risk_states = risk_states
        self._exposure_rows = exposure_rows
        self._pending_rows = pending_rows or []

    def fetch_latest_risk_state_per_account(self) -> list[dict[str, Any]]:
        return list(self._risk_states)

    def aggregate_open_positions_by_account_symbol(self) -> list[dict[str, Any]]:
        return list(self._exposure_rows)

    def aggregate_pending_orders_by_account_symbol(self) -> list[dict[str, Any]]:
        return list(self._pending_rows)


class _FakeSignalModule:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.page_kwargs: dict[str, Any] | None = None

    def recent_signal_page(self, **kwargs: Any) -> dict[str, Any]:
        self.page_kwargs = kwargs
        return {
            "items": self._rows,
            "total": len(self._rows),
            "page": 1,
            "page_size": 10,
        }


def _build_model(
    *,
    risk_states: list[dict[str, Any]] | None = None,
    exposure_rows: list[dict[str, Any]] | None = None,
    pending_rows: list[dict[str, Any]] | None = None,
    signals: list[dict[str, Any]] | None = None,
) -> CockpitReadModel:
    return CockpitReadModel(
        trading_state_repo=_FakeTradingStateRepo(
            risk_states=risk_states or [],
            exposure_rows=exposure_rows or [],
            pending_rows=pending_rows,
        ),
        signal_module=_FakeSignalModule(signals),
        runtime_read_model=None,
    )


def test_cockpit_returns_all_seven_blocks() -> None:
    model = _build_model(risk_states=[], exposure_rows=[])
    payload = model.build_overview()

    for block in COCKPIT_BLOCKS:
        assert block in payload
    assert payload["source"]["kind"] == "live"


def test_decision_verdict_blocked_when_any_account_blocks_new_trades() -> None:
    model = _build_model(
        risk_states=[
            {
                "account_alias": "live_main",
                "account_key": "live:broker:1001",
                "should_block_new_trades": True,
                "circuit_open": False,
                "close_only_mode": False,
                "should_emergency_close": False,
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "live_exec_a",
                "account_key": "live:broker:1002",
                "should_block_new_trades": False,
                "circuit_open": False,
                "close_only_mode": False,
                "should_emergency_close": False,
                "updated_at": _now_iso(),
            },
        ]
    )
    payload = model.build_overview(include=["decision"])

    decision = payload["decision"]
    assert decision["can_trade"] is False
    assert decision["verdict"] == "blocked"
    assert "block_new_trades" in decision["reason_codes"]
    assert "live_main" in decision["blocked_accounts"]


def test_triage_queue_classifies_and_sorts_by_severity() -> None:
    model = _build_model(
        risk_states=[
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "should_block_new_trades": False,
                "circuit_open": False,
                "close_only_mode": False,
                "should_emergency_close": False,
                "active_risk_flags": [],
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "should_emergency_close": True,
                "circuit_open": True,
                "should_block_new_trades": True,
                "close_only_mode": True,
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "demo_main",
                "account_key": "demo:1",
                "close_only_mode": True,
                "should_block_new_trades": False,
                "circuit_open": False,
                "should_emergency_close": False,
                "updated_at": _now_iso(),
            },
        ]
    )
    payload = model.build_overview(include=["triage_queue"])

    triage = payload["triage_queue"]
    entries = triage["entries"]
    assert entries[0]["account_alias"] == "live_main"
    assert entries[0]["priority_layer"] == TRIAGE_LAYER_CRITICAL
    assert entries[0]["reason_code"] == "emergency_close_required"
    # warn 层（demo close-only）排第二
    assert entries[1]["account_alias"] == "demo_main"
    assert entries[1]["priority_layer"] == TRIAGE_LAYER_WARN
    # 健康账户最后
    assert entries[-1]["priority_layer"] == TRIAGE_LAYER_HEALTHY


def test_exposure_map_freshness_reflects_underlying_row_updated_at() -> None:
    """P3 回归：exposure_map.data_updated_at 旧实现写成 observed_at（now），
    所以底层数据明显陈旧（如 6 小时前 row）时仍报 freshness_state='fresh'。

    修复：data_updated_at 必须取 exposure_rows + pending_rows 的 MAX(updated_at)，
    反映"底层数据最近一次刷新到什么时候"。无 updated_at 字段时回退到 observed_at -
    max_age_seconds 让 freshness 至少标 stale（fail-closed）。
    """
    six_hours_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    model = _build_model(
        exposure_rows=[
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.5,
                "position_count": 1,
                # 6 小时前更新；与 exposure_map 的 max_age_seconds=90 相比明显陈旧
                "data_updated_at": six_hours_ago.isoformat(),
            },
        ]
    )
    payload = model.build_overview(include=["exposure_map"])
    block = payload["exposure_map"]
    # data_updated_at 必须取自 row 的 6 小时前时间，不是 observed_at（now）
    assert block["data_updated_at"] != block["observed_at"], (
        f"data_updated_at 应反映底层 row updated_at，"
        f"不应等于 observed_at；got data_updated_at={block['data_updated_at']!r}"
    )
    # freshness 必须不是 fresh（陈旧 6 小时 >> max_age_seconds=90）
    assert (
        block["freshness_state"] != "fresh"
    ), f"6 小时陈旧数据不应被标 fresh；block={block!r}"


def test_exposure_map_aggregates_contributors_across_accounts() -> None:
    model = _build_model(
        exposure_rows=[
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.6,
                "position_count": 3,
            },
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.4,
                "position_count": 2,
            },
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "EURUSD",
                "direction": "sell",
                "gross_volume": 1.0,
                "position_count": 1,
            },
        ]
    )
    payload = model.build_overview(include=["exposure_map"])

    entries = payload["exposure_map"]["entries"]
    # XAUUSD/buy 应合并为 2 contributors
    xau_bucket = next(
        e for e in entries if e["symbol"] == "XAUUSD" and e["direction"] == "buy"
    )
    assert xau_bucket["gross_exposure"] == pytest.approx(1.0)
    assert len(xau_bucket["contributors"]) == 2
    weights = {c["account_alias"]: c["weight"] for c in xau_bucket["contributors"]}
    assert weights["live_main"] == pytest.approx(0.6)
    assert weights["live_exec_a"] == pytest.approx(0.4)
    # P12-1: exposure_map 同时暴露 mode + risk_matrix，即便无 pending
    assert payload["exposure_map"]["mode"] == "account_symbol"
    assert isinstance(payload["exposure_map"]["risk_matrix"], list)


def test_exposure_map_risk_matrix_is_account_major_with_net_and_pending() -> None:
    """P12-1: risk_matrix[] 按账户分组；同品种 buy/sell 聚合为 net=buy-sell，gross=buy+sell；pending 独立求和。"""
    model = _build_model(
        exposure_rows=[
            # live_main XAUUSD buy 0.6 + sell 0.2 → gross=0.8, net=+0.4
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.6,
                "position_count": 3,
            },
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "XAUUSD",
                "direction": "sell",
                "gross_volume": 0.2,
                "position_count": 1,
            },
            # live_main EURUSD sell 1.0
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "EURUSD",
                "direction": "sell",
                "gross_volume": 1.0,
                "position_count": 1,
            },
            # live_exec_a XAUUSD buy 0.4
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.4,
                "position_count": 2,
            },
        ],
        pending_rows=[
            # live_main XAUUSD buy pending 0.15
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "symbol": "XAUUSD",
                "direction": "buy",
                "pending_volume": 0.15,
                "pending_count": 1,
            },
            # live_exec_a GBPUSD buy pending 0.05（没有 open 持仓，仅 pending）
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "symbol": "GBPUSD",
                "direction": "buy",
                "pending_volume": 0.05,
                "pending_count": 1,
            },
        ],
    )
    payload = model.build_overview(include=["exposure_map"])
    matrix = payload["exposure_map"]["risk_matrix"]

    by_account = {row["account_alias"]: row for row in matrix}
    assert set(by_account.keys()) == {"live_main", "live_exec_a"}

    # live_main: XAUUSD gross=0.8/net=+0.4/pending=0.15; EURUSD gross=1.0/net=-1.0
    main = by_account["live_main"]
    assert main["total_risk"] == pytest.approx(1.8)  # 0.8 + 1.0
    cells_by_symbol = {c["symbol"]: c for c in main["cells"]}
    xau = cells_by_symbol["XAUUSD"]
    assert xau["gross_exposure"] == pytest.approx(0.8)
    assert xau["net_exposure"] == pytest.approx(0.4)
    assert xau["pending_exposure"] == pytest.approx(0.15)
    assert xau["position_count"] == 4
    assert xau["pending_count"] == 1
    # risk_score = gross / total_risk
    assert xau["risk_score"] == pytest.approx(0.8 / 1.8)

    eur = cells_by_symbol["EURUSD"]
    assert eur["net_exposure"] == pytest.approx(-1.0)
    assert eur["pending_exposure"] == pytest.approx(0.0)

    # live_exec_a：XAUUSD 仅 open（无 pending）；GBPUSD 仅 pending（无 open）
    exec_a = by_account["live_exec_a"]
    assert exec_a["total_risk"] == pytest.approx(0.4)  # 仅 open 参与 total_risk
    exec_cells = {c["symbol"]: c for c in exec_a["cells"]}
    assert exec_cells["XAUUSD"]["gross_exposure"] == pytest.approx(0.4)
    assert exec_cells["XAUUSD"]["pending_exposure"] == pytest.approx(0.0)
    gbp = exec_cells["GBPUSD"]
    assert gbp["gross_exposure"] == pytest.approx(0.0)
    assert gbp["net_exposure"] == pytest.approx(0.0)
    assert gbp["pending_exposure"] == pytest.approx(0.05)
    assert gbp["risk_score"] == pytest.approx(0.0)  # gross 为 0 → 占比 0


def test_exposure_map_risk_matrix_total_risk_zero_handles_division() -> None:
    """P12-1: total_risk=0（账户全部仓位 volume=0）时 risk_score 兜底为 0，不得 ZeroDivision。"""
    model = _build_model(
        exposure_rows=[
            {
                "account_alias": "live_idle",
                "account_key": "live:0",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.0,
                "position_count": 0,
            },
        ]
    )
    payload = model.build_overview(include=["exposure_map"])
    matrix = payload["exposure_map"]["risk_matrix"]
    row = matrix[0]
    assert row["total_risk"] == pytest.approx(0.0)
    assert row["cells"][0]["risk_score"] == pytest.approx(0.0)


def test_exposure_map_risk_matrix_sorted_by_total_risk_desc() -> None:
    """P12-1: 矩阵按 total_risk 从大到小排序，最忙账户在前。"""
    model = _build_model(
        exposure_rows=[
            {
                "account_alias": "a_small",
                "account_key": "k:a",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 0.1,
                "position_count": 1,
            },
            {
                "account_alias": "b_big",
                "account_key": "k:b",
                "symbol": "XAUUSD",
                "direction": "buy",
                "gross_volume": 2.0,
                "position_count": 5,
            },
        ]
    )
    payload = model.build_overview(include=["exposure_map"])
    aliases = [row["account_alias"] for row in payload["exposure_map"]["risk_matrix"]]
    assert aliases == ["b_big", "a_small"]


class _FakeIntelReadModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_action_queue(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "observed_at": "2026-04-20T10:00:00+00:00",
            "entries": [
                {
                    "id": "sig_x",
                    "signal_id": "sig_x",
                    "account_candidates": [{"account_alias": "live_main"}],
                    "recommended_action": "execute_from_signal",
                }
            ],
            "pagination": {"page": 1, "page_size": 10, "total": 1},
            "freshness": {"data_updated_at": "2026-04-20T09:59:00+00:00"},
        }


def test_opportunity_queue_delegates_to_intel_when_injected() -> None:
    """P1.3: 注入 IntelReadModel 后，opportunity_queue 直接返回 Intel 丰富后的 entries。"""
    intel = _FakeIntelReadModel()
    model = CockpitReadModel(
        trading_state_repo=_FakeTradingStateRepo(risk_states=[], exposure_rows=[]),
        signal_module=_FakeSignalModule([]),  # 不应该被调用
        runtime_read_model=None,
        intel_read_model=intel,
    )

    payload = model.build_overview(include=["opportunity_queue"])

    queue = payload["opportunity_queue"]
    assert len(queue["entries"]) == 1
    assert queue["entries"][0]["recommended_action"] == "execute_from_signal"
    assert queue["entries"][0]["account_candidates"] == [{"account_alias": "live_main"}]
    # 确认有委托调用
    assert len(intel.calls) == 1
    assert intel.calls[0]["page_size"] == model.DEFAULT_MAX_OPPORTUNITY


def test_opportunity_queue_uses_priority_desc_and_actionable_filter() -> None:
    signals = [
        {
            "signal_id": "sig_a",
            "trace_id": "trace_a",
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "strategy": "breakout_follow",
            "direction": "buy",
            "confidence": 0.72,
            "priority": 0.72,
            "rank_source": "native",
            "generated_at": _now_iso(),
        }
    ]
    signal_module = _FakeSignalModule(signals)
    model = CockpitReadModel(
        trading_state_repo=_FakeTradingStateRepo(risk_states=[], exposure_rows=[]),
        signal_module=signal_module,
        runtime_read_model=None,
    )

    payload = model.build_overview(include=["opportunity_queue"])

    assert signal_module.page_kwargs is not None
    assert signal_module.page_kwargs["actionability"] == "actionable"
    assert signal_module.page_kwargs["sort"] == "priority_desc"
    assert len(payload["opportunity_queue"]["entries"]) == 1


def test_safe_actions_prioritizes_emergency_over_block() -> None:
    model = _build_model(
        risk_states=[
            {
                "account_alias": "live_main",
                "account_key": "live:1",
                "should_emergency_close": True,
                "circuit_open": True,
                "updated_at": _now_iso(),
            },
            {
                "account_alias": "live_exec_a",
                "account_key": "live:2",
                "should_emergency_close": False,
                "circuit_open": True,
                "updated_at": _now_iso(),
            },
        ]
    )
    payload = model.build_overview(include=["safe_actions"])

    actions = payload["safe_actions"]["entries"]
    live_main_actions = [a for a in actions if a["account_alias"] == "live_main"]
    # live_main 只记 emergency（更高优先级），不记 circuit
    assert len(live_main_actions) == 1
    assert live_main_actions[0]["action"] == "emergency_close_all"
    # live_exec_a 记 circuit review
    live_exec_actions = [a for a in actions if a["account_alias"] == "live_exec_a"]
    assert live_exec_actions[0]["action"] == "review_circuit_breaker"


def test_cockpit_include_csv_limits_blocks() -> None:
    model = _build_model(risk_states=[], exposure_rows=[])

    payload = model.build_overview(include=["decision", "market_guard"])

    assert "decision" in payload
    assert "market_guard" in payload
    assert "exposure_map" not in payload
    assert "opportunity_queue" not in payload
