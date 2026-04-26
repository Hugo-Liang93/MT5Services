from __future__ import annotations

from datetime import datetime, timezone

from src.readmodels.trade_trace import TradingFlowTraceReadModel


class _SignalRepo:
    def fetch_signal_event_by_id(self, *, signal_id: str, scope: str = "confirmed"):
        if scope == "preview":
            return {
                "generated_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "strategy": "trendline",
                "direction": "buy",
                "confidence": 0.62,
                "metadata": {"signal_trace_id": "trace-1"},
            }
        return {
            "generated_at": datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
            "signal_id": signal_id,
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "strategy": "trendline",
            "direction": "buy",
            "confidence": 0.68,
            "metadata": {"signal_trace_id": "trace-1"},
        }

    def fetch_auto_executions(
        self, *, signal_id: str, limit: int = 50, account_alias=None
    ):
        return [
            {
                "executed_at": datetime(2026, 1, 1, 8, 16, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "account_alias": account_alias or "live",
                "symbol": "XAUUSD",
                "direction": "buy",
                "strategy": "trendline",
                "success": True,
            }
        ]

    def fetch_signal_outcomes(self, *, signal_id: str, limit: int = 20):
        return []

    def fetch_trade_outcomes(
        self, *, signal_id: str, limit: int = 20, account_alias=None
    ):
        return [
            {
                "recorded_at": datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "account_alias": account_alias or "live",
                "won": True,
                "price_change": 10.5,
            }
        ]

    def fetch_signal_events_by_trace_id(
        self,
        *,
        trace_id: str,
        scope: str = "confirmed",
        limit: int = 50,
    ):
        rows = [
            {
                "generated_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                "signal_id": "sig-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "strategy": "trendline",
                "direction": "buy",
                "confidence": 0.62,
                "metadata": {"signal_trace_id": trace_id},
            }
        ]
        if scope == "preview":
            return rows
        return [
            {
                **rows[0],
                "generated_at": datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
                "confidence": 0.68,
            }
        ]


class _TradeRepo:
    def fetch_trace_operations(self, *, account_alias: str, signal_id: str, limit: int = 100):
        return [
            {
                "recorded_at": datetime(2026, 1, 1, 8, 16, 30, tzinfo=timezone.utc),
                "operation_id": "op-1",
                "command_type": "execute_trade",
                "status": "success",
                "request_payload": {"request_id": signal_id},
                "response_payload": {
                    "request_id": signal_id,
                    "ticket": 7001,
                },
                "ticket": 7001,
                "order_id": 7001,
                "deal_id": 9001,
            }
        ]

    def fetch_trace_operations_by_trace_id(self, *, account_alias: str, trace_id: str, limit: int = 100):
        return []


class _PipelineTraceRepo:
    def fetch_pipeline_trace_events(self, *, trace_ids, limit: int = 500):
        assert trace_ids == ["trace-1"]
        return [
            {
                "id": 1,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "bar_closed",
                "recorded_at": datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc),
                "payload": {"bar_time": "2026-01-01T07:59:00+00:00"},
            },
            {
                "id": 2,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "indicator_computed",
                "recorded_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                "payload": {"indicator_count": 5},
            },
            {
                "id": 3,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "snapshot_published",
                "recorded_at": datetime(2026, 1, 1, 8, 0, 5, tzinfo=timezone.utc),
                "payload": {"indicator_count": 5},
            },
            {
                "id": 4,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "signal_filter_decided",
                "recorded_at": datetime(2026, 1, 1, 8, 0, 10, tzinfo=timezone.utc),
                "payload": {"allowed": True, "category": "_pass", "reason": ""},
            },
            {
                "id": 5,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "signal_evaluated",
                "recorded_at": datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
                "payload": {
                    "strategy": "trendline",
                    "direction": "buy",
                    "signal_state": "confirmed",
                },
            },
            {
                "id": 6,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "admission_report_appended",
                "recorded_at": datetime(2026, 1, 1, 8, 15, 20, tzinfo=timezone.utc),
                "payload": {
                    "decision": "allow",
                    "stage": "account_risk",
                    "trace_id": "trace-1",
                    "signal_id": "sig-1",
                    "reasons": [{"code": "checks_passed", "message": "ok"}],
                },
            },
            {
                "id": 7,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "execution_decided",
                "recorded_at": datetime(2026, 1, 1, 8, 15, 30, tzinfo=timezone.utc),
                "payload": {
                    "strategy": "trendline",
                    "direction": "buy",
                    "order_kind": "market",
                },
            },
            {
                "id": 8,
                "trace_id": "trace-1",
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "scope": "confirmed",
                "event_type": "execution_submitted",
                "recorded_at": datetime(2026, 1, 1, 8, 16, tzinfo=timezone.utc),
                "payload": {
                    "strategy": "trendline",
                    "direction": "buy",
                    "order_kind": "market",
                    "request_id": "sig-1",
                    "ticket": 7001,
                },
            },
        ]

    def query_trace_summaries(self, **kwargs):
        return {
            "items": [
                {
                    "trace_id": kwargs.get("trace_id") or "trace-1",
                    "signal_id": kwargs.get("signal_id") or "sig-1",
                    "symbol": kwargs.get("symbol") or "XAUUSD",
                    "timeframe": kwargs.get("timeframe") or "M15",
                    "strategy": kwargs.get("strategy") or "trendline",
                    "status": kwargs.get("status") or "submitted",
                    "last_admission_decision": "allow",
                    "last_admission_stage": "account_risk",
                    "started_at": datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc),
                    "last_event_at": datetime(2026, 1, 1, 8, 16, tzinfo=timezone.utc),
                    "event_count": 8,
                    "last_event_type": "execution_submitted",
                    "reason": "ok",
                }
            ],
            "total": 3,
            "page": kwargs.get("page") or 1,
            "page_size": kwargs.get("page_size") or 100,
        }


class _TradingStateRepo:
    def fetch_pending_order_states(self, *, account_alias=None, statuses=None, signal_id=None, limit=500):
        return [
            {
                "order_ticket": 7001,
                "signal_id": signal_id,
                "status": "filled",
                "filled_at": datetime(2026, 1, 1, 8, 17, tzinfo=timezone.utc),
                "position_ticket": 8001,
            }
        ]

    def fetch_position_runtime_states(self, *, account_alias=None, statuses=None, signal_id=None, limit=500):
        return [
            {
                "position_ticket": 8001,
                "order_ticket": 7001,
                "signal_id": signal_id,
                "status": "closed",
                "opened_at": datetime(2026, 1, 1, 8, 17, tzinfo=timezone.utc),
                "closed_at": datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
            }
        ]


def test_trade_trace_projection_aggregates_signal_to_outcome_chain() -> None:
    read_model = TradingFlowTraceReadModel(
        signal_repo=_SignalRepo(),
        command_audit_repo=_TradeRepo(),
        pipeline_trace_repo=_PipelineTraceRepo(),
        trading_state_repo=_TradingStateRepo(),
        account_alias_getter=lambda: "live",
    )

    trace = read_model.trace_by_signal_id("sig-1")

    assert trace["found"] is True
    assert trace["identifiers"]["signal_id"] == "sig-1"
    assert trace["identifiers"]["trace_ids"] == ["trace-1"]
    assert trace["identifiers"]["order_tickets"] == [7001]
    assert trace["identifiers"]["position_tickets"] == [8001]
    assert trace["summary"]["stages"]["pipeline_bar_closed"] == "present"
    assert trace["summary"]["stages"]["pipeline_signal_filter"] == "present"
    assert trace["summary"]["stages"]["pipeline_admission"] == "present"
    assert trace["summary"]["stages"]["pipeline_execution_decision"] == "present"
    assert trace["summary"]["stages"]["pipeline_execution_submission"] == "present"
    assert trace["summary"]["pipeline_event_counts"]["signal_filter_decided"] == 1
    assert trace["summary"]["pipeline_event_counts"]["admission_report_appended"] == 1
    assert trace["summary"]["pipeline_event_counts"]["execution_submitted"] == 1
    assert trace["summary"]["admission"]["decision"] == "allow"
    assert trace["summary"]["admission"]["stage"] == "account_risk"
    assert trace["summary"]["stages"]["confirmed_signal"] == "present"
    assert trace["summary"]["pending_status_counts"]["filled"] == 1
    assert trace["summary"]["position_status_counts"]["closed"] == 1
    assert trace["summary"]["command_counts"]["execute_trade"] == 1
    assert trace["summary"]["status"] == "completed"
    assert trace["summary"]["last_stage"] == "outcome.trade"
    assert trace["facts"]["admission_reports"][0]["decision"] == "allow"
    assert trace["timeline"][0]["stage"] == "pipeline.bar_closed"
    assert trace["timeline"][-1]["stage"] == "outcome.trade"
    assert trace["related_trade_audits"][0]["operation_id"] == "op-1"
    assert trace["related_pipeline_events"][0]["trace_id"] == "trace-1"
    assert len(trace["graph"]["nodes"]) == len(trace["timeline"])
    assert len(trace["graph"]["edges"]) == len(trace["timeline"]) - 1


def test_trade_trace_projection_supports_trace_id_only_chain() -> None:
    read_model = TradingFlowTraceReadModel(
        signal_repo=_SignalRepo(),
        command_audit_repo=_TradeRepo(),
        pipeline_trace_repo=_PipelineTraceRepo(),
        trading_state_repo=_TradingStateRepo(),
        account_alias_getter=lambda: "live",
    )

    trace = read_model.trace_by_trace_id("trace-1")

    assert trace["found"] is True
    assert trace["signal_id"] == "sig-1"
    assert trace["trace_id"] == "trace-1"
    assert trace["identifiers"]["trace_ids"] == ["trace-1"]
    assert trace["identifiers"]["signal_ids"] == ["sig-1"]
    assert trace["timeline"][0]["stage"] == "pipeline.bar_closed"
    assert trace["summary"]["stages"]["pipeline_signal_filter"] == "present"
    assert trace["summary"]["stages"]["pipeline_admission"] == "present"
    assert trace["summary"]["stages"]["pipeline_execution_decision"] == "present"
    assert trace["summary"]["status"] == "completed"
    assert trace["summary"]["admission"]["decision"] == "allow"


def test_trade_trace_directory_projection_exposes_trace_summary_list() -> None:
    read_model = TradingFlowTraceReadModel(
        signal_repo=_SignalRepo(),
        command_audit_repo=_TradeRepo(),
        pipeline_trace_repo=_PipelineTraceRepo(),
        trading_state_repo=_TradingStateRepo(),
        account_alias_getter=lambda: "live",
    )

    result = read_model.list_traces(
        symbol="XAUUSD",
        timeframe="M15",
        status="submitted",
        page=2,
        page_size=20,
    )

    assert result["total"] == 3
    assert result["page"] == 2
    assert result["page_size"] == 20
    assert result["items"][0]["trace_id"] == "trace-1"
    assert result["items"][0]["status"] == "submitted"
    assert result["items"][0]["last_stage"] == "pipeline.execution_submitted"
    assert result["items"][0]["admission"]["decision"] == "allow"
    assert result["items"][0]["admission"]["stage"] == "account_risk"


def test_trade_trace_reason_extractor_reads_nested_execution_skip_result() -> None:
    reason = TradingFlowTraceReadModel._extract_reason(
        None,
        [
            {
                "event_type": "execution_skipped",
                "payload": {
                    "status": "skipped",
                    "result": {
                        "status": "skipped",
                        "reason": "trade_params_unavailable",
                        "skip_category": "trade_params",
                    },
                },
            }
        ],
    )

    assert reason == "trade_params_unavailable"


# ── §0v P2 + P3 回归：trade_trace 跨账户泄漏 + 去重丢同刻事件 ──


class _MultiAccountSignalRepo:
    """模拟同 signal_id 被多个账户执行的场景。"""

    def fetch_signal_event_by_id(self, *, signal_id: str, scope: str = "confirmed"):
        return None

    def fetch_signal_events_by_trace_id(self, *, trace_id: str, scope="confirmed", limit=50):
        return []

    def fetch_auto_executions(
        self,
        *,
        signal_id: str,
        limit: int = 50,
        account_alias=None,
    ):
        rows = [
            {
                "executed_at": datetime(2026, 1, 1, 8, 16, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "account_alias": "acct_a",
                "account_key": "live:srv:111",
                "symbol": "XAUUSD",
                "direction": "buy",
                "success": True,
            },
            {
                "executed_at": datetime(2026, 1, 1, 8, 16, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "account_alias": "acct_b",
                "account_key": "live:srv:222",
                "symbol": "XAUUSD",
                "direction": "buy",
                "success": True,
            },
        ]
        if account_alias is not None:
            rows = [r for r in rows if r["account_alias"] == account_alias]
        return rows

    def fetch_signal_outcomes(self, *, signal_id: str, limit: int = 20):
        return []

    def fetch_trade_outcomes(
        self,
        *,
        signal_id: str,
        limit: int = 20,
        account_alias=None,
    ):
        rows = [
            {
                "recorded_at": datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "account_alias": "acct_a",
                "account_key": "live:srv:111",
                "won": True,
            },
            {
                "recorded_at": datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
                "signal_id": signal_id,
                "account_alias": "acct_b",
                "account_key": "live:srv:222",
                "won": False,
            },
        ]
        if account_alias is not None:
            rows = [r for r in rows if r["account_alias"] == account_alias]
        return rows


class _EmptyTradeRepo:
    def fetch_trace_operations(self, *, account_alias, signal_id, limit=100):
        return []

    def fetch_trace_operations_by_trace_id(self, *, account_alias, trace_id, limit=100):
        return []


class _EmptyPipelineTraceRepo:
    def fetch_pipeline_trace_events(self, *, trace_ids, limit=500):
        return []


class _EmptyTradingStateRepo:
    def fetch_pending_order_states(self, *, account_alias=None, statuses=None, signal_id=None, limit=500):
        return []

    def fetch_position_runtime_states(self, *, account_alias=None, statuses=None, signal_id=None, limit=500):
        return []


def test_trace_by_signal_id_does_not_mix_other_accounts_executions_and_outcomes() -> None:
    """P2 §0v 回归：旧 trace_by_signal_id 对 auto_executions / trade_outcomes
    只按 signal_id 拉全局数据 → 多账户共享 signal_id 时把别人的执行/结果混进
    当前账户 facts/timeline。
    """
    read_model = TradingFlowTraceReadModel(
        signal_repo=_MultiAccountSignalRepo(),
        command_audit_repo=_EmptyTradeRepo(),
        pipeline_trace_repo=_EmptyPipelineTraceRepo(),
        trading_state_repo=_EmptyTradingStateRepo(),
        account_alias_getter=lambda: "acct_a",
    )

    trace = read_model.trace_by_signal_id("sig-1")

    auto = trace["facts"]["auto_executions"]
    outcomes = trace["facts"]["trade_outcomes"]
    aliases_auto = {row.get("account_alias") for row in auto}
    aliases_outcome = {row.get("account_alias") for row in outcomes}

    assert aliases_auto == {"acct_a"}, (
        f"trace 只能含当前账户 auto_executions；got aliases={aliases_auto!r}"
    )
    assert aliases_outcome == {"acct_a"}, (
        f"trace 只能含当前账户 trade_outcomes；got aliases={aliases_outcome!r}"
    )


def test_fetch_by_signal_ids_dedup_preserves_cross_account_same_timestamp_events() -> None:
    """P3 §0v 回归：旧 _fetch_by_signal_ids 去重键仅 (signal_id, timestamp)，
    无 account 维度 → 两账户同时刻同 signal_id 的执行/结果 → 第二条静默被丢。
    即使将来加上账户过滤，这个 helper 仍需具备正确处理跨账户同刻事件的能力。
    """
    rows_in = [
        {
            "signal_id": "sig-1",
            "executed_at": "2026-01-01T08:16:00+00:00",
            "account_alias": "acct_a",
            "account_key": "live:srv:111",
            "symbol": "XAUUSD",
        },
        {
            "signal_id": "sig-1",
            "executed_at": "2026-01-01T08:16:00+00:00",
            "account_alias": "acct_b",
            "account_key": "live:srv:222",
            "symbol": "XAUUSD",
        },
    ]

    def _fetcher(*, signal_id, limit, account_alias=None):
        return list(rows_in)

    rows_out = TradingFlowTraceReadModel._fetch_by_signal_ids(
        signal_ids=["sig-1"],
        fetcher=_fetcher,
        limit=10,
    )
    aliases = {row.get("account_alias") for row in rows_out}
    assert aliases == {"acct_a", "acct_b"}, (
        f"两账户同时刻不同 row 不能被去重掉一个；got aliases={aliases!r}, rows_out={rows_out!r}"
    )
