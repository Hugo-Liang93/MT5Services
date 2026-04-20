"""交易工作台读模型（P10.3）。

面向 QuantX `Trades` 页面与 `Trade Detail` 抽屉，提供：
- `/v1/trades/workbench`：基于 `trade_outcomes` 的交易记录分页 + 摘要
- `/v1/trades/{trade_id}`：按 signal_id 组装 6 维 trade detail
  （plan_vs_live / lifecycle / risk_review / receipts / evidence / linked_account_state）

与 `TradingFlowTraceReadModel` 的区别：
- trade_trace 以 trace_id/signal_id 为入口，返回信号管线完整 timeline
- trades_workbench 以 signal_id 为 trade_id，按 6 个业务维度重组 trace，
  供前端 Trade 抽屉直接渲染，不需要再做字段拼接
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from src.readmodels.freshness import build_freshness_block


class TradesWorkbenchReadModel:
    """trades/workbench canonical 读模型（P10.3）。"""

    DEFAULT_LIST_MAX_AGE_SECONDS: int = 60
    DEFAULT_LIST_STALE_AFTER_SECONDS: int = 15

    def __init__(
        self,
        *,
        signal_repo: Any,
        trace_read_model: Any,
        account_alias_getter: Callable[[], str],
    ) -> None:
        self._signal_repo = signal_repo
        self._trace_read_model = trace_read_model
        self._account_alias_getter = account_alias_getter

    # ────────────── /v1/trades/workbench ──────────────
    def build_workbench(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        won: Optional[bool] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 50,
        sort: str = "recorded_at_desc",
    ) -> dict[str, Any]:
        """分页列出该账户的交易结果 + 统计摘要 + freshness。"""
        account_alias = self._account_alias_getter()
        observed_at = datetime.now(timezone.utc).isoformat()
        list_result = self._signal_repo.query_trade_outcomes_page(
            account_alias=account_alias,
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            won=won,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        summary = self._signal_repo.fetch_trade_outcomes_summary(
            account_alias=account_alias,
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            from_time=from_time,
            to_time=to_time,
        )
        records = [self._build_record(item) for item in list_result.get("items") or []]
        pagination = {
            "page": int(list_result.get("page") or page),
            "page_size": int(list_result.get("page_size") or page_size),
            "total": int(list_result.get("total") or 0),
            "sort": sort,
        }
        freshness = self._build_list_freshness(
            observed_at=observed_at,
            last_recorded_at=summary.get("last_recorded_at"),
        )
        return {
            "account_alias": account_alias,
            "observed_at": observed_at,
            "records": records,
            "summary": summary,
            "pagination": pagination,
            "freshness": freshness,
            "filters": {
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy": strategy,
                "direction": direction,
                "won": won,
                "from": from_time.isoformat() if from_time else None,
                "to": to_time.isoformat() if to_time else None,
            },
        }

    # ────────────── /v1/trades/{trade_id} ──────────────
    def build_trade_detail(self, *, trade_id: str) -> Optional[dict[str, Any]]:
        """P10.3 trade detail：按 signal_id 组装 6 维 canonical 视图。

        **职责边界声明（P2.1）**：本方法**不是独立事实源**，而是
        `TradingFlowTraceReadModel.trace_by_signal_id()` 的**视图适配器**。
        它按前端 `Trade Detail` 抽屉的 6 维契约（plan_vs_live / lifecycle /
        risk_review / receipts / evidence / linked_account_state）重新分组 trace
        facts，不做任何额外 DB 查询。

        依赖的 trace_payload 契约字段（若上游 TradingFlowTraceReadModel 结构变更，
        需同步更新此处）：
          - trace_payload["found"]: bool
          - trace_payload["trace_id"]: str | None
          - trace_payload["identifiers"]: dict
          - trace_payload["summary"]: dict
          - trace_payload["timeline"]: list[dict]
          - trace_payload["facts"]["signal_confirmed" | "signal_preview"]: dict
          - trace_payload["facts"]["auto_executions"]: list[dict]
          - trace_payload["facts"]["trade_command_audits"]: list[dict]
          - trace_payload["facts"]["pending_orders"]: list[dict]
          - trace_payload["facts"]["positions"]: list[dict]
          - trace_payload["facts"]["trade_outcomes"]: list[dict]
          - trace_payload["facts"]["signal_outcomes"]: list[dict]
          - trace_payload["facts"]["pipeline_trace_events"]: list[dict]
          - trace_payload["facts"]["admission_reports"]: list[dict]
        """
        normalized = str(trade_id or "").strip()
        if not normalized:
            return None
        trace_payload = self._trace_read_model.trace_by_signal_id(normalized)
        if not trace_payload or not trace_payload.get("found"):
            return None

        facts = trace_payload.get("facts") or {}
        timeline = list(trace_payload.get("timeline") or [])
        trade_outcomes = list(facts.get("trade_outcomes") or [])
        signal_outcomes = list(facts.get("signal_outcomes") or [])
        auto_executions = list(facts.get("auto_executions") or [])
        command_audits = list(facts.get("trade_command_audits") or [])
        pending_orders = list(facts.get("pending_orders") or [])
        positions = list(facts.get("positions") or [])
        pipeline_events = list(facts.get("pipeline_trace_events") or [])
        admission_reports = list(facts.get("admission_reports") or [])

        plan_signal = facts.get("signal_confirmed") or facts.get("signal_preview")

        observed_at = datetime.now(timezone.utc).isoformat()
        return {
            "trade_id": normalized,
            "signal_id": normalized,
            "trace_id": trace_payload.get("trace_id"),
            "observed_at": observed_at,
            "identifiers": trace_payload.get("identifiers") or {},
            "summary": trace_payload.get("summary") or {},
            "plan_vs_live": self._build_plan_vs_live(
                plan_signal=plan_signal,
                auto_executions=auto_executions,
                trade_outcomes=trade_outcomes,
                positions=positions,
            ),
            "lifecycle": self._build_lifecycle(
                timeline=timeline,
                pending_orders=pending_orders,
                positions=positions,
            ),
            "risk_review": self._build_risk_review(
                admission_reports=admission_reports,
                pipeline_events=pipeline_events,
            ),
            "receipts": self._build_receipts(command_audits=command_audits),
            "evidence": self._build_evidence(
                plan_signal=plan_signal,
                signal_outcomes=signal_outcomes,
            ),
            "linked_account_state": self._build_linked_account_state(
                auto_executions=auto_executions,
                admission_reports=admission_reports,
            ),
            "freshness": {
                "observed_at": observed_at,
                "source_kind": "native",
                "fallback_applied": False,
            },
        }

    # ────────────── 内部构造 ──────────────
    @staticmethod
    def _build_record(row: dict[str, Any]) -> dict[str, Any]:
        fill_price = row.get("fill_price")
        close_price = row.get("close_price")
        price_change = row.get("price_change")
        pnl_percent = None
        if fill_price not in (None, 0) and price_change is not None:
            try:
                pnl_percent = float(price_change) / float(fill_price) * 100.0
            except (TypeError, ZeroDivisionError, ValueError):
                pnl_percent = None
        return {
            "trade_id": row.get("signal_id"),
            "signal_id": row.get("signal_id"),
            "recorded_at": row.get("recorded_at"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy": row.get("strategy"),
            "direction": row.get("direction"),
            "confidence": row.get("confidence"),
            "account_alias": row.get("account_alias"),
            "account_key": row.get("account_key"),
            "intent_id": row.get("intent_id"),
            "fill_price": fill_price,
            "close_price": close_price,
            "price_change": price_change,
            "pnl_percent": pnl_percent,
            "won": row.get("won"),
            "regime": row.get("regime"),
        }

    def _build_list_freshness(
        self,
        *,
        observed_at: str,
        last_recorded_at: Optional[str],
    ) -> dict[str, Any]:
        return build_freshness_block(
            observed_at=observed_at,
            data_updated_at=last_recorded_at,
            stale_after_seconds=self.DEFAULT_LIST_STALE_AFTER_SECONDS,
            max_age_seconds=self.DEFAULT_LIST_MAX_AGE_SECONDS,
        )

    @staticmethod
    def _build_plan_vs_live(
        *,
        plan_signal: Optional[dict[str, Any]],
        auto_executions: Sequence[dict[str, Any]],
        trade_outcomes: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        plan: dict[str, Any] = {}
        if plan_signal:
            metadata = plan_signal.get("metadata") or {}
            entry_spec = metadata.get("entry_spec") or {}
            plan = {
                "direction": plan_signal.get("direction"),
                "confidence": plan_signal.get("confidence"),
                "strategy": plan_signal.get("strategy"),
                "entry_price": entry_spec.get("entry_price") or entry_spec.get("price"),
                "entry_type": entry_spec.get("entry_type"),
                "stop_loss": entry_spec.get("stop_loss"),
                "take_profit": entry_spec.get("take_profit"),
                "volume": entry_spec.get("volume"),
                "risk_reward": metadata.get("risk_reward"),
                "generated_at": plan_signal.get("generated_at"),
            }
        live: dict[str, Any] = {}
        if auto_executions:
            first_exec = auto_executions[0]
            live = {
                "intent_id": first_exec.get("intent_id"),
                "entry_price": first_exec.get("entry_price"),
                "stop_loss": first_exec.get("stop_loss"),
                "take_profit": first_exec.get("take_profit"),
                "volume": first_exec.get("volume"),
                "success": first_exec.get("success"),
                "error_message": first_exec.get("error_message"),
                "executed_at": first_exec.get("executed_at"),
            }
        outcome: dict[str, Any] = {}
        if trade_outcomes:
            last_outcome = trade_outcomes[-1]
            outcome = {
                "fill_price": last_outcome.get("fill_price"),
                "close_price": last_outcome.get("close_price"),
                "price_change": last_outcome.get("price_change"),
                "won": last_outcome.get("won"),
                "regime": last_outcome.get("regime"),
                "recorded_at": last_outcome.get("recorded_at"),
            }
        position_state: Optional[dict[str, Any]] = None
        if positions:
            last_pos = positions[-1]
            position_state = {
                "ticket": last_pos.get("ticket"),
                "volume": last_pos.get("volume"),
                "open_price": last_pos.get("open_price"),
                "current_price": last_pos.get("current_price"),
                "sl": last_pos.get("sl"),
                "tp": last_pos.get("tp"),
                "status": last_pos.get("status"),
                "profit": last_pos.get("profit"),
            }
        return {
            "plan": plan or None,
            "live": live or None,
            "outcome": outcome or None,
            "position_state": position_state,
        }

    @staticmethod
    def _build_lifecycle(
        *,
        timeline: Sequence[dict[str, Any]],
        pending_orders: Sequence[dict[str, Any]],
        positions: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "timeline": list(timeline),
            "pending_orders": list(pending_orders),
            "positions": list(positions),
        }

    @staticmethod
    def _build_risk_review(
        *,
        admission_reports: Sequence[dict[str, Any]],
        pipeline_events: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        reports = list(admission_reports)
        last_decision = None
        last_stage = None
        if reports:
            last = reports[-1]
            payload = last.get("payload") or last
            last_decision = payload.get("decision") or payload.get("verdict")
            last_stage = payload.get("stage")
        risk_block_events = [
            event
            for event in pipeline_events
            if str(event.get("event_type") or "").lower().find("block") >= 0
            or str(event.get("event_type") or "").lower().find("risk") >= 0
        ]
        return {
            "admission_reports": reports,
            "last_decision": last_decision,
            "last_stage": last_stage,
            "risk_events": risk_block_events,
        }

    @staticmethod
    def _build_receipts(
        *,
        command_audits: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for audit in command_audits:
            request_payload = audit.get("request_payload") or {}
            response_payload = audit.get("response_payload") or {}
            entries.append(
                {
                    "audit_id": audit.get("operation_id"),
                    "command_type": audit.get("command_type"),
                    "status": audit.get("status"),
                    "recorded_at": (
                        audit.get("recorded_at").isoformat()
                        if hasattr(audit.get("recorded_at"), "isoformat")
                        else audit.get("recorded_at")
                    ),
                    "symbol": audit.get("symbol"),
                    "side": audit.get("side"),
                    "order_kind": audit.get("order_kind"),
                    "volume": audit.get("volume"),
                    "ticket": audit.get("ticket"),
                    "order_id": audit.get("order_id"),
                    "deal_id": audit.get("deal_id"),
                    "action_id": request_payload.get("action_id")
                    or response_payload.get("action_id"),
                    "idempotency_key": request_payload.get("idempotency_key")
                    or response_payload.get("idempotency_key"),
                    "actor": request_payload.get("actor"),
                    "reason": request_payload.get("reason"),
                    "error_message": audit.get("error_message"),
                }
            )
        return {
            "entries": entries,
            "count": len(entries),
        }

    @staticmethod
    def _build_evidence(
        *,
        plan_signal: Optional[dict[str, Any]],
        signal_outcomes: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = {}
        rationale = None
        if plan_signal:
            metadata = plan_signal.get("metadata") or {}
            rationale = plan_signal.get("reason") or metadata.get("rationale")
        indicator_snapshot = metadata.get("indicators") if metadata else None
        return {
            "rationale": rationale,
            "strategy": plan_signal.get("strategy") if plan_signal else None,
            "indicator_snapshot": indicator_snapshot,
            "metadata": metadata,
            "signal_outcomes": list(signal_outcomes),
        }

    @staticmethod
    def _build_linked_account_state(
        *,
        auto_executions: Sequence[dict[str, Any]],
        admission_reports: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        """提取该笔交易关联的账户上下文。

        **P2.2 修复**：旧实现扫 pipeline_events.payload.account_risk_state/account_snapshot
        这两个 key，但全代码库都不写入这两个 key —— 永远是空壳。改为从真实可用的事实源
        提取：
        - auto_executions → 该 signal 被哪些账户执行（account_alias / account_key）
        - admission_reports → 该 signal 的 admission 阶段快照（含 account_state 字段，
          若上游 admission service 有填）

        **未来扩展**（需单独设计，不在 TradesWorkbench 范围）：要拿到 signal.generated_at
        前后的 account_risk_state 快照，需要在 TradingFlowTraceReadModel 内注入
        trading_state_repo 做 `fetch_account_risk_states(account_alias=..., from=..., to=...)`
        查询，并在 trace_payload.facts.linked_account_snapshots 中透出。
        """
        executing_accounts: list[dict[str, Any]] = []
        seen_aliases: set[str] = set()
        for execution in auto_executions:
            alias = str(execution.get("account_alias") or "").strip()
            if not alias or alias in seen_aliases:
                continue
            seen_aliases.add(alias)
            executing_accounts.append(
                {
                    "account_alias": alias,
                    "account_key": execution.get("account_key"),
                    "intent_id": execution.get("intent_id"),
                    "executed_at": execution.get("executed_at"),
                    "success": execution.get("success"),
                }
            )
        admission_snapshots: list[dict[str, Any]] = []
        for report in admission_reports:
            payload = report.get("payload") or report
            account_state = (
                payload.get("account_state")
                or payload.get("account_snapshot")
                or payload.get("risk_state")
            )
            if account_state:
                admission_snapshots.append(
                    {
                        "stage": payload.get("stage"),
                        "decision": payload.get("decision") or payload.get("verdict"),
                        "account_state": account_state,
                    }
                )
        return {
            "executing_accounts": executing_accounts,
            "admission_snapshots": admission_snapshots,
            "count": len(executing_accounts) + len(admission_snapshots),
            "note": (
                "linked_account_state is reconstructed from auto_executions + "
                "admission_reports; point-in-time account_risk_state snapshots are "
                "not yet populated (pending trace readmodel extension)."
            ),
        }
