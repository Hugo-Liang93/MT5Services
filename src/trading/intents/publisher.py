from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable
from uuid import uuid4

from src.config.mt5 import MT5Settings, load_group_mt5_settings
from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.signals.contracts import StrategyDeployment
from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent
from src.trading.intents.codec import signal_event_to_payload

logger = logging.getLogger(__name__)


class ExecutionIntentPublisher:
    def __init__(
        self,
        *,
        write_fn,
        runtime_identity: RuntimeIdentity,
        reserve_intent_key_fn=None,
        account_bindings: dict[str, list[str]] | None = None,
        strategy_deployments: dict[str, StrategyDeployment] | None = None,
        auto_trade_enabled: bool = True,
        pipeline_event_bus: PipelineEventBus | None = None,
    ) -> None:
        self._write_fn = write_fn
        # §0dm P2 #5：reserve_intent_key_fn 在 _write_fn 之前调用，atomic
        # 跨时间唯一注册 intent_key（hypertable UNIQUE 跨 chunk 限制下的 ledger
        # 兜底）。None 时退化为旧行为（仅 hypertable 内 ON CONFLICT 防重）。
        self._reserve_intent_key_fn = reserve_intent_key_fn
        self._runtime_identity = runtime_identity
        self._pipeline_event_bus = pipeline_event_bus
        self._accounts = load_group_mt5_settings(
            instance_name=runtime_identity.instance_name,
        )
        self.update_bindings(
            account_bindings=account_bindings,
            strategy_deployments=strategy_deployments,
            auto_trade_enabled=auto_trade_enabled,
        )

    def update_bindings(
        self,
        *,
        account_bindings: dict[str, list[str]] | None,
        strategy_deployments: dict[str, StrategyDeployment] | None,
        auto_trade_enabled: bool,
    ) -> None:
        self._account_bindings = {
            alias: {strategy for strategy in strategies if str(strategy).strip()}
            for alias, strategies in (account_bindings or {}).items()
        }
        self._strategy_deployments = dict(strategy_deployments or {})
        self._auto_trade_enabled = bool(auto_trade_enabled)

    def on_signal_event(self, event: SignalEvent) -> None:
        if not self._auto_trade_enabled or self._runtime_identity.instance_role != "main":
            return
        if not self._is_actionable_signal_event(event):
            return
        if not event.signal_id:
            return

        # §0dl P2：旧实现循环内先 _emit_intent_published 再 _write_fn → 写库
        # 失败时 pipeline/trace 已发出 intent_published 事件，但队列表无对应
        # intent，形成不可追溯的幽灵发布。修复：先收集 row + emit_args，写库
        # atomic 完成后再批量 emit；写库失败异常透出，event bus 不发任何事件。
        # §0dm P2 #5：每条 intent_key 先走 ledger reserve（跨时间 UNIQUE）；
        # 已存在则跳过该 row（防止并发重复发布同 signal_id+account 组合）。
        published_at = datetime.now(timezone.utc)
        generated_at = (
            event.generated_at
            if event.generated_at.tzinfo
            else event.generated_at.replace(tzinfo=timezone.utc)
        )
        target_rows = []
        emit_args_list: list[dict[str, Any]] = []
        for account in self._resolve_target_accounts(event.strategy):
            target_account_key = build_account_key(
                self._runtime_identity.environment,
                account.mt5_server,
                account.mt5_login,
            )
            intent_id = uuid4().hex
            intent_key = f"{event.signal_id}:{target_account_key}"
            # §0dm P2 #5：ledger 注册——失败说明已重复发布，跳过该 row。
            if self._reserve_intent_key_fn is not None:
                reserved = self._reserve_intent_key_fn(
                    intent_key=intent_key,
                    intent_id=intent_id,
                    created_at=generated_at,
                    signal_id=event.signal_id,
                    target_account_key=target_account_key,
                )
                if not reserved:
                    logger.warning(
                        "ExecutionIntentPublisher: intent_key=%s already reserved "
                        "(duplicate publish suppressed); signal_id=%s account=%s",
                        intent_key,
                        event.signal_id,
                        account.account_alias,
                    )
                    continue
            target_rows.append(
                (
                    generated_at,
                    intent_id,
                    intent_key,
                    event.signal_id,
                    target_account_key,
                    account.account_alias,
                    event.strategy,
                    event.symbol,
                    event.timeframe,
                    signal_event_to_payload(event),
                    "pending",
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    {
                        "published_by_instance_id": self._runtime_identity.instance_id,
                        "published_at": published_at.isoformat(),
                        "signal_state": event.signal_state,
                    },
                )
            )
            emit_args_list.append(
                {
                    "event": event,
                    "intent_id": intent_id,
                    "intent_key": intent_key,
                    "target_account_key": target_account_key,
                    "target_account_alias": account.account_alias,
                    "published_at": published_at,
                }
            )
        if not target_rows:
            return

        # 持久化必须先成功，才允许 emit pipeline 事件——否则形成幽灵发布。
        self._write_fn(target_rows)
        for emit_args in emit_args_list:
            self._emit_intent_published(**emit_args)

    @staticmethod
    def _is_actionable_signal_event(event: SignalEvent) -> bool:
        scope = str(event.scope or "").strip().lower()
        signal_state = str(event.signal_state or "").strip().lower()
        direction = str(event.direction or "").strip().lower()
        if direction not in {"buy", "sell"}:
            return False
        if scope == "confirmed":
            return "confirmed" in signal_state
        if scope == "intrabar":
            return signal_state.startswith("intrabar_armed_")
        return False

    def _resolve_target_accounts(self, strategy: str) -> Iterable[MT5Settings]:
        # §0dj：environment-aware 门禁的唯一入口是 deployment.is_executable_in()，
        # 与装配层 _filter_strategies_for_environment / pre_trade_checks 同一合同。
        # 旧实现各处 if env == "demo" else 兜底分支已废除（§0dd → §0dg → §0dj）。
        deployment = self._strategy_deployments.get(strategy)
        if deployment is None:
            return ()
        if not deployment.is_executable_in(self._runtime_identity.environment):
            return ()

        explicit_aliases = [
            alias
            for alias, strategies in self._account_bindings.items()
            if strategy in strategies
        ]

        for alias in explicit_aliases:
            account = self._accounts.get(alias)
            if account is None:
                logger.warning(
                    "ExecutionIntentPublisher: account binding alias not configured: %s",
                    alias,
                )
                continue
            yield account

    def _emit_intent_published(
        self,
        *,
        event: SignalEvent,
        intent_id: str,
        intent_key: str,
        target_account_key: str,
        target_account_alias: str,
        published_at: datetime,
    ) -> None:
        if self._pipeline_event_bus is None:
            return
        trace_id = (
            str(
                (event.metadata or {}).get(MK.SIGNAL_TRACE_ID)
                or (event.metadata or {}).get("trace_id")
                or event.signal_id
                or intent_id
            ).strip()
        )
        self._pipeline_event_bus.emit(
            PipelineEvent(
                type="intent_published",
                trace_id=trace_id,
                symbol=event.symbol,
                timeframe=event.timeframe,
                scope=event.scope,
                ts=published_at.isoformat(),
                payload={
                    "signal_id": event.signal_id,
                    "intent_id": intent_id,
                    "intent_key": intent_key,
                    "trace_id": trace_id,
                    "strategy": event.strategy,
                    "direction": event.direction,
                    "signal_scope": event.scope,
                    "signal_state": event.signal_state,
                    "target_account_key": target_account_key,
                    "target_account_alias": target_account_alias,
                    "published_by_instance_id": self._runtime_identity.instance_id,
                    # §0dl P3：旧实现把 instance_id 顶替 run_id（与 §0dk consumer
                    # 同模式 schema 语义违反，§0dk 漏修 publisher）。改 .run_id
                    # 让 run 级 ownership 在 pipeline trace 里真正区分。
                    "published_by_run_id": self._runtime_identity.run_id,
                    "instance_role": self._runtime_identity.instance_role,
                },
            )
        )
