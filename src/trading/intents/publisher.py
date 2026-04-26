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
        account_bindings: dict[str, list[str]] | None = None,
        strategy_deployments: dict[str, StrategyDeployment] | None = None,
        auto_trade_enabled: bool = True,
        pipeline_event_bus: PipelineEventBus | None = None,
    ) -> None:
        self._write_fn = write_fn
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

        published_at = datetime.now(timezone.utc)
        target_rows = []
        for account in self._resolve_target_accounts(event.strategy):
            target_account_key = build_account_key(
                self._runtime_identity.environment,
                account.mt5_server,
                account.mt5_login,
            )
            intent_id = uuid4().hex
            intent_key = f"{event.signal_id}:{target_account_key}"
            target_rows.append(
                (
                    event.generated_at
                    if event.generated_at.tzinfo
                    else event.generated_at.replace(tzinfo=timezone.utc),
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
            self._emit_intent_published(
                event=event,
                intent_id=intent_id,
                intent_key=intent_key,
                target_account_key=target_account_key,
                target_account_alias=account.account_alias,
                published_at=published_at,
            )
        if not target_rows:
            return

        self._write_fn(target_rows)

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
        # §0dd P1：旧实现硬性要求 deployment.allows_live_execution() →
        # demo_validation 策略在 demo 环境装配后仍永远不发 intent，demo 真实
        # 下单验证职责完全断链。改按 runtime_identity.environment 路由，
        # 与装配层 _filter_strategies_for_environment 的过滤口径对齐：
        #   - environment="demo" → allows_demo_validation (含 active/active_guarded/demo_validation)
        #   - environment="live" → allows_live_execution (仅 active/active_guarded)
        # 这样装配什么 publisher 就发什么，不重复门控；防 live 误装漂移仍由
        # 装配层守门。
        deployment = self._strategy_deployments.get(strategy)
        if deployment is None:
            return ()
        environment = str(self._runtime_identity.environment or "").strip().lower()
        if environment == "demo":
            allowed = deployment.allows_demo_validation()
        else:
            # live 环境（默认）保持 allows_live_execution 严格门控
            allowed = deployment.allows_live_execution()
        if not allowed:
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
                    "published_by_run_id": self._runtime_identity.instance_id,
                    "instance_role": self._runtime_identity.instance_role,
                },
            )
        )
