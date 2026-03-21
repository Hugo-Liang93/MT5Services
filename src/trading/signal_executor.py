"""TradeExecutor: auto-executes trades in response to confirmed signal events.

Lives in the trading module, not the signal module, to maintain clean separation:
- Signal module  → generates and publishes SignalEvent (knows nothing about trading)
- TradeExecutor  → subscribes via SignalRuntime.add_signal_listener(), executes trades

Usage in deps.py:
    executor = TradeExecutor(trading_module=_c.trade_module, config=cfg)
    signal_runtime.add_signal_listener(executor.on_signal_event)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
import dataclasses as _dc
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from src.trading.sizing import (
    TradeParameters,
    compute_trade_params,
    extract_atr_from_indicators,
)
from src.signals.models import SignalEvent
from src.signals.strategies.htf_cache import HTFStateCache
from src.trading.position_manager import PositionManager
from src.trading.trade_outcome_tracker import TradeOutcomeTracker

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    enabled: bool = False
    min_confidence: float = 0.7
    require_armed: bool = True
    max_concurrent_positions_per_symbol: int | None = 3
    risk_percent: float = 1.0
    sl_atr_multiplier: float = 1.5
    tp_atr_multiplier: float = 3.0
    min_volume: float = 0.01
    max_volume: float = 1.0
    # T-2: 按品种配置合约大小，替代全局固定值（XAUUSD=100, BTCUSD=1, 等）
    contract_size_map: Dict[str, float] = field(
        default_factory=lambda: {"XAUUSD": 100.0, "default": 100.0}
    )
    default_volume: float = 0.01
    # 熔断器：连续失败超过此阈值后自动暂停自动交易
    max_consecutive_failures: int = 3
    # T-3: 自动半开恢复：熔断后等待 N 分钟再自动尝试
    circuit_auto_reset_minutes: int = 30
    max_spread_to_stop_ratio: float = 0.33
    # HTF 对齐修正：若启用，冲突时软惩罚，一致时轻微加成。
    htf_filter_enabled: bool = True
    htf_conflict_penalty: float = 0.70
    htf_alignment_boost: float = 1.10
    # 交易触发白名单：仅允许列表内的策略触发实际下单。
    # 空元组 = 不限制，所有策略均可触发（默认兼容行为）。
    # 非空时，白名单外的策略信号仍正常产生（用于监控/投票），但不执行交易。
    # 推荐配置：["consensus"] 或 ["consensus", "trend_triple_confirm"]
    trade_trigger_strategies: tuple[str, ...] = field(default_factory=tuple)
    # 分组保护：属于某个 voting group 的策略集合（union of all groups）。
    # 非空时，这些策略不允许单独触发交易——只能通过其 vote group 结果触发。
    # 空 frozenset = 不启用分组保护（兼容旧行为）。
    voting_group_strategies: frozenset[str] = field(default_factory=frozenset)
    # standalone_override：即使属于某个 voting group，仍允许单独触发交易的策略白名单。
    standalone_override: frozenset[str] = field(default_factory=frozenset)


class TradeExecutor:
    """Subscribes to SignalRuntime events and auto-executes confirmed trades."""

    def __init__(
        self,
        trading_module: Any,
        config: Optional[ExecutorConfig] = None,
        account_balance_getter: Optional[Any] = None,
        position_manager: Optional[PositionManager] = None,
        htf_cache: Optional[HTFStateCache] = None,
        persist_execution_fn: Optional[Callable[[List], None]] = None,
        trade_outcome_tracker: Optional[TradeOutcomeTracker] = None,
    ):
        self._trading = trading_module
        self.config = config or ExecutorConfig()
        self._account_balance_getter = account_balance_getter
        self._position_manager = position_manager
        self._htf_cache = htf_cache
        # T-4: 执行记录持久化回调（可选，用于写入 auto_executions 表）
        self._persist_execution_fn = persist_execution_fn
        self._trade_outcome_tracker = trade_outcome_tracker
        self._execution_count = 0
        self._last_execution_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._execution_log: list[dict] = []
        # 熔断器状态
        self._consecutive_failures: int = 0
        self._circuit_open: bool = False
        # T-3: 记录熔断开路时间，用于自动恢复检查
        self._circuit_open_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public listener interface
    # ------------------------------------------------------------------

    def on_signal_event(self, event: SignalEvent) -> None:
        """Called by SignalRuntime for every signal state transition.

        Only acts on scope=confirmed transitions with a buy/sell action.
        """
        if event.scope != "confirmed":
            return
        if "confirmed" not in event.signal_state:
            return
        self._handle_confirmed(event)

    # ------------------------------------------------------------------
    # Internal execution logic
    # ------------------------------------------------------------------

    def reset_circuit(self) -> None:
        """手动重置熔断器，恢复自动交易。"""
        self._circuit_open = False
        self._consecutive_failures = 0
        self._circuit_open_at = None
        logger.info("TradeExecutor: circuit breaker manually reset")

    def _get_contract_size(self, symbol: str) -> float:
        """T-2: 按品种返回合约大小，优先精确匹配，fallback 到 'default'。"""
        size_map = self.config.contract_size_map
        return size_map.get(symbol, size_map.get("default", 100.0))

    def _handle_confirmed(self, event: SignalEvent) -> Optional[Dict[str, Any]]:
        if not self.config.enabled:
            return None

        # ── 熔断器检查（含 T-3 自动半开恢复）────────────────────────────
        if self._circuit_open:
            # T-3: 若距熔断开路已超过 circuit_auto_reset_minutes，自动尝试半开
            if (
                self.config.circuit_auto_reset_minutes > 0
                and self._circuit_open_at is not None
            ):
                elapsed = (
                    datetime.now(timezone.utc) - self._circuit_open_at
                ).total_seconds() / 60.0
                if elapsed >= self.config.circuit_auto_reset_minutes:
                    logger.info(
                        "TradeExecutor: circuit auto-reset after %.1f minutes, "
                        "attempting half-open",
                        elapsed,
                    )
                    self.reset_circuit()
            if self._circuit_open:
                logger.warning(
                    "TradeExecutor: circuit open (consecutive_failures=%d), "
                    "skipping %s/%s. Call reset_circuit() to resume.",
                    self._consecutive_failures, event.symbol, event.strategy,
                )
                return None

        if event.action not in ("buy", "sell"):
            return None

        # ── Voting Group 分组保护 ─────────────────────────────────────
        # 若策略属于某个 voting group，则默认不允许单独触发交易；
        # 只有该 group 的 vote 结果信号（strategy = group_name）才能触发。
        # standalone_override 白名单中的策略可豁免此限制。
        if (
            self.config.voting_group_strategies
            and event.strategy in self.config.voting_group_strategies
            and event.strategy not in self.config.standalone_override
        ):
            logger.debug(
                "TradeExecutor: skipping %s/%s %s - strategy %r is in a voting group "
                "and not in standalone_override (use vote result signal to trigger)",
                event.symbol, event.timeframe, event.action, event.strategy,
            )
            return None

        # ── 交易触发白名单过滤 ────────────────────────────────────────
        # trade_trigger_strategies 非空时，仅允许白名单内的策略触发下单。
        # 白名单外的策略信号仍正常产生（持久化、监控、投票均不受影响），
        # 只是不在此处执行实际交易，避免多策略同向重复开仓。
        allowed = self.config.trade_trigger_strategies
        if allowed and event.strategy not in allowed:
            logger.debug(
                "TradeExecutor: skipping %s/%s %s - strategy %r not in "
                "trade_trigger_strategies whitelist",
                event.symbol, event.timeframe, event.action, event.strategy,
            )
            return None

        # ── require_armed 检查 ────────────────────────────────────────
        # confirmed 事件的 previous_state 是上一个 confirmed_state（idle/confirmed_buy），
        # 永远不含 "armed"。因此同时检查 preview_state_at_close（bar 收盘时的盘中状态），
        # 该字段由 runtime._transition_confirmed 在清除 preview 状态前注入。
        if self.config.require_armed:
            previous_state = event.metadata.get("previous_state", "")
            preview_at_close = event.metadata.get("preview_state_at_close", "")
            if "armed" not in previous_state and "armed" not in preview_at_close:
                logger.info(
                    "TradeExecutor: skipping %s/%s %s - require_armed=True but "
                    "previous_state=%r, preview_state_at_close=%r",
                    event.symbol, event.strategy, event.action,
                    previous_state, preview_at_close,
                )
                return None

        # ── HTF 方向软惩罚 ────────────────────────────────────────────
        if self.config.htf_filter_enabled and self._htf_cache is not None:
            htf_direction = self._htf_cache.get_htf_direction(event.symbol, event.timeframe)
            if htf_direction is not None:
                multiplier = (
                    self.config.htf_alignment_boost
                    if htf_direction == event.action
                    else self.config.htf_conflict_penalty
                )
                event = _dc.replace(
                    event,
                    confidence=min(1.0, event.confidence * multiplier),
                    metadata={
                        **event.metadata,
                        "htf_direction": htf_direction,
                        "htf_alignment": (
                            "aligned" if htf_direction == event.action else "conflict"
                        ),
                        "htf_confidence_multiplier": multiplier,
                    },
                )

        if event.confidence < self.config.min_confidence:
            logger.info(
                "TradeExecutor: skipping %s/%s %s - confidence %.3f < min=%.2f",
                event.symbol,
                event.strategy,
                event.action,
                event.confidence,
                self.config.min_confidence,
            )
            return None

        if self._reached_position_limit(event.symbol):
            logger.info(
                "TradeExecutor: skipping %s/%s %s - max_concurrent_positions_per_symbol reached",
                event.symbol,
                event.strategy,
                event.action,
            )
            self._execution_log.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": event.signal_id,
                    "symbol": event.symbol,
                    "action": event.action,
                    "strategy": event.strategy,
                    "success": False,
                    "skipped": True,
                    "reason": "max_concurrent_positions_per_symbol",
                }
            )
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]
            return None

        trade_params = self._compute_params(event)
        if trade_params is None:
            atr = extract_atr_from_indicators(event.indicators)
            balance = self._get_account_balance()
            close_price = event.metadata.get("close_price") or self._estimate_price(event.indicators)
            logger.warning(
                "TradeExecutor: cannot compute trade params for %s/%s %s "
                "(atr=%s, balance=%s, close_price=%s, indicators_keys=%s)",
                event.symbol, event.strategy, event.action,
                atr, balance, close_price,
                list(event.indicators.keys()),
            )
            return None
        cost_metrics = self._estimate_cost_metrics(event, trade_params)
        spread_to_stop_ratio = cost_metrics.get("spread_to_stop_ratio")
        if (
            spread_to_stop_ratio is not None
            and spread_to_stop_ratio > self.config.max_spread_to_stop_ratio
        ):
            logger.info(
                "TradeExecutor: skipping %s/%s %s - spread_to_stop_ratio %.3f > max=%.3f",
                event.symbol,
                event.strategy,
                event.action,
                spread_to_stop_ratio,
                self.config.max_spread_to_stop_ratio,
            )
            self._execution_log.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": event.signal_id,
                    "symbol": event.symbol,
                    "action": event.action,
                    "strategy": event.strategy,
                    "success": False,
                    "skipped": True,
                    "reason": "spread_to_stop_ratio_too_high",
                    "cost": cost_metrics,
                }
            )
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]
            return None

        return self._execute(event, trade_params, cost_metrics=cost_metrics)

    def _compute_params(self, event: SignalEvent) -> Optional[TradeParameters]:
        atr = extract_atr_from_indicators(event.indicators)
        if atr is None or atr <= 0:
            return None

        balance = self._get_account_balance()
        if balance is None or balance <= 0:
            return None

        # T-1: 优先使用 runtime 注入的 close_price（策略域收窄前提取，所有策略均有效）
        close_price: Optional[float] = None
        raw_close = event.metadata.get("close_price")
        if raw_close is not None:
            try:
                close_price = float(raw_close)
            except (TypeError, ValueError):
                close_price = None
        # fallback：扫描指标 payload（旧路径）
        if close_price is None or close_price <= 0:
            close_price = self._estimate_price(event.indicators)
        if close_price is None or close_price <= 0:
            return None

        # T-2: 按品种读取合约大小
        contract_size = self._get_contract_size(event.symbol)

        return compute_trade_params(
            action=event.action,
            current_price=close_price,
            atr_value=atr,
            account_balance=balance,
            timeframe=event.timeframe,
            risk_percent=self.config.risk_percent,
            sl_atr_multiplier=self.config.sl_atr_multiplier,
            tp_atr_multiplier=self.config.tp_atr_multiplier,
            min_volume=self.config.min_volume,
            max_volume=self.config.max_volume,
            contract_size=contract_size,
        )

    def _get_account_balance(self) -> Optional[float]:
        if self._account_balance_getter is not None:
            try:
                return float(self._account_balance_getter())
            except Exception:
                pass
        try:
            info = self._trading.account_info()
            if isinstance(info, dict):
                return float(info.get("equity") or info.get("balance") or 0)
            return float(getattr(info, "equity", None) or getattr(info, "balance", None) or 0)
        except Exception:
            return None

    @staticmethod
    def _estimate_price(indicators: Dict[str, Dict[str, Any]]) -> Optional[float]:
        for name in ("bollinger20", "sma20", "close", "price"):
            payload = indicators.get(name)
            if isinstance(payload, dict):
                for fld in ("close", "value", "last", "bb_mid", "sma"):
                    val = payload.get(fld)
                    if val is not None:
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            continue
        return None

    def _reached_position_limit(self, symbol: str) -> bool:
        limit = self.config.max_concurrent_positions_per_symbol
        if limit is None or limit <= 0:
            return False
        open_positions = self._open_positions_for_symbol(symbol)
        return open_positions >= limit

    def _open_positions_for_symbol(self, symbol: str) -> int:
        if self._position_manager is not None:
            try:
                tracked = [
                    row
                    for row in self._position_manager.active_positions()
                    if row.get("symbol") == symbol
                ]
                return len(tracked)
            except Exception:
                pass

        for attr_name in ("get_positions", "positions"):
            getter = getattr(self._trading, attr_name, None)
            if not callable(getter):
                continue
            try:
                rows = getter(symbol=symbol)
            except TypeError:
                rows = getter(symbol)
            except Exception:
                continue
            try:
                return len(list(rows or []))
            except Exception:
                continue
        return 0

    def _estimate_cost_metrics(
        self,
        event: SignalEvent,
        params: TradeParameters,
    ) -> Dict[str, Optional[float]]:
        raw_spread = event.metadata.get("spread_points")
        try:
            spread_points = float(raw_spread) if raw_spread is not None else None
        except (TypeError, ValueError):
            spread_points = None

        raw_symbol_point = event.metadata.get("symbol_point")
        try:
            symbol_point = (
                float(raw_symbol_point) if raw_symbol_point is not None else None
            )
        except (TypeError, ValueError):
            symbol_point = None
        if symbol_point is not None and symbol_point <= 0:
            symbol_point = None

        raw_spread_price = event.metadata.get("spread_price")
        try:
            spread_price = (
                float(raw_spread_price) if raw_spread_price is not None else None
            )
        except (TypeError, ValueError):
            spread_price = None
        if spread_price is None and spread_points is not None and symbol_point is not None:
            spread_price = spread_points * symbol_point
        if spread_points is None and spread_price is not None and symbol_point is not None:
            spread_points = spread_price / symbol_point

        raw_close = event.metadata.get("close_price")
        try:
            close_price = float(raw_close) if raw_close is not None else None
        except (TypeError, ValueError):
            close_price = None
        if close_price is None or close_price <= 0:
            close_price = self._estimate_price(event.indicators)

        stop_distance = None
        reward_distance = None
        if close_price is not None and close_price > 0:
            stop_distance = abs(close_price - params.stop_loss)
            reward_distance = abs(params.take_profit - close_price)

        stop_distance_points = None
        reward_distance_points = None
        if symbol_point is not None and symbol_point > 0:
            if stop_distance is not None:
                stop_distance_points = stop_distance / symbol_point
            if reward_distance is not None:
                reward_distance_points = reward_distance / symbol_point

        spread_to_stop_ratio = None
        if spread_points is not None:
            if stop_distance_points is not None and stop_distance_points > 0:
                spread_to_stop_ratio = round(spread_points / stop_distance_points, 4)
            elif spread_price is not None and stop_distance and stop_distance > 0:
                spread_to_stop_ratio = round(spread_price / stop_distance, 4)

        reward_to_cost_ratio = None
        if spread_points is not None and spread_points > 0:
            if reward_distance_points is not None:
                reward_to_cost_ratio = round(
                    reward_distance_points / spread_points, 4
                )
            elif spread_price is not None and spread_price > 0 and reward_distance is not None:
                reward_to_cost_ratio = round(reward_distance / spread_price, 4)

        return {
            "estimated_cost_points": spread_points,
            "estimated_cost_price": (
                round(spread_price, 6) if spread_price is not None else None
            ),
            "symbol_point": symbol_point,
            "stop_distance": round(stop_distance, 4) if stop_distance is not None else None,
            "stop_distance_points": (
                round(stop_distance_points, 2)
                if stop_distance_points is not None
                else None
            ),
            "reward_distance": round(reward_distance, 4) if reward_distance is not None else None,
            "reward_distance_points": (
                round(reward_distance_points, 2)
                if reward_distance_points is not None
                else None
            ),
            "spread_to_stop_ratio": spread_to_stop_ratio,
            "reward_to_cost_ratio": reward_to_cost_ratio,
        }

    @staticmethod
    def _build_trade_metadata(event: SignalEvent) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "signal": {
                "signal_id": event.signal_id,
                "strategy": event.strategy,
                "timeframe": event.timeframe,
                "signal_state": event.signal_state,
                "confidence": round(float(event.confidence), 4),
            }
        }
        raw_structure = event.metadata.get("market_structure")
        if isinstance(raw_structure, dict):
            metadata["market_structure"] = dict(raw_structure)
        elif hasattr(raw_structure, "to_dict"):
            try:
                metadata["market_structure"] = raw_structure.to_dict()
            except Exception:
                pass
        return metadata

    def _execute(
        self,
        event: SignalEvent,
        params: TradeParameters,
        *,
        cost_metrics: Optional[Dict[str, Optional[float]]] = None,
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "symbol": event.symbol,
            "volume": params.position_size,
            "side": event.action,
            "order_kind": "market",
            "sl": params.stop_loss,
            "tp": params.take_profit,
            "comment": f"auto:{event.strategy}:{event.action}",
            "metadata": self._build_trade_metadata(event),
        }

        try:
            result = self._trading.dispatch_operation("trade", payload)
            self._execution_count += 1
            self._last_execution_at = datetime.now(timezone.utc)
            self._last_error = None
            # 成功执行：重置熔断器失败计数
            self._consecutive_failures = 0
            log_entry = {
                "at": self._last_execution_at.isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "action": event.action,
                "strategy": event.strategy,
                "confidence": event.confidence,
                "params": {
                    "volume": params.position_size,
                    "sl": params.stop_loss,
                    "tp": params.take_profit,
                    "rr": params.risk_reward_ratio,
                },
                "cost": dict(cost_metrics or {}),
                "success": True,
            }
            self._execution_log.append(log_entry)
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]
            logger.info(
                "TradeExecutor: executed %s %s vol=%.2f sl=%.2f tp=%.2f rr=%.2f (signal=%s)",
                event.action, event.symbol,
                params.position_size, params.stop_loss, params.take_profit,
                params.risk_reward_ratio, event.signal_id,
            )
            # T-4: 持久化执行记录
            if self._persist_execution_fn is not None:
                try:
                    self._persist_execution_fn([log_entry])
                except Exception as pe:
                    logger.warning("TradeExecutor: persist execution failed: %s", pe)
            if self._position_manager is not None and isinstance(result, dict):
                ticket = result.get("ticket") or result.get("order")
                if ticket:
                    try:
                        self._position_manager.track_position(
                            ticket=int(ticket),
                            signal_id=event.signal_id,
                            symbol=event.symbol,
                            action=event.action,
                            params=params,
                        )
                    except Exception as pm_exc:
                        logger.warning(
                            "TradeExecutor: failed to register position ticket=%s: %s",
                            ticket, pm_exc,
                        )
            # 通知 TradeOutcomeTracker 登记活跃交易
            if self._trade_outcome_tracker is not None:
                try:
                    self._trade_outcome_tracker.on_trade_opened(
                        signal_id=event.signal_id,
                        symbol=event.symbol,
                        timeframe=event.timeframe,
                        strategy=event.strategy,
                        action=event.action,
                        fill_price=params.entry_price,
                        confidence=event.confidence,
                        regime=event.metadata.get("regime"),
                    )
                except Exception as ot_exc:
                    logger.warning(
                        "TradeExecutor: failed to notify trade_outcome_tracker: %s",
                        ot_exc,
                    )
            return result
        except Exception as exc:
            self._last_error = str(exc)
            self._consecutive_failures += 1
            self._execution_log.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "action": event.action,
                "strategy": event.strategy,
                "success": False,
                "error": str(exc),
            })
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]
            logger.exception(
                "TradeExecutor: failed to execute %s %s: %s", event.action, event.symbol, exc,
            )
            # 熔断器：连续失败达到阈值后开路，防止持续错误下的无效重试
            if (
                not self._circuit_open
                and self._consecutive_failures >= self.config.max_consecutive_failures
            ):
                self._circuit_open = True
                self._circuit_open_at = datetime.now(timezone.utc)
                logger.error(
                    "TradeExecutor: circuit breaker OPENED after %d consecutive failures. "
                    "Auto-trading suspended. Will auto-reset in %d minutes or call reset_circuit().",
                    self._consecutive_failures,
                    self.config.circuit_auto_reset_minutes,
                )
            # T-4: 持久化失败记录
            fail_entry = {
                "at": datetime.now(timezone.utc).isoformat(),
                "signal_id": event.signal_id,
                "symbol": event.symbol,
                "action": event.action,
                "strategy": event.strategy,
                "success": False,
                "error": str(exc),
            }
            if self._persist_execution_fn is not None:
                try:
                    self._persist_execution_fn([fail_entry])
                except Exception as pe:
                    logger.warning("TradeExecutor: persist fail-entry failed: %s", pe)
            return None

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "execution_count": self._execution_count,
            "last_execution_at": self._last_execution_at.isoformat() if self._last_execution_at else None,
            "last_error": self._last_error,
            "circuit_breaker": {
                "open": self._circuit_open,
                "consecutive_failures": self._consecutive_failures,
                "max_consecutive_failures": self.config.max_consecutive_failures,
                "circuit_open_at": self._circuit_open_at.isoformat() if self._circuit_open_at else None,
                "auto_reset_minutes": self.config.circuit_auto_reset_minutes,
            },
            "config": {
                "min_confidence": self.config.min_confidence,
                "require_armed": self.config.require_armed,
                "max_concurrent_positions_per_symbol": self.config.max_concurrent_positions_per_symbol,
                "htf_filter_enabled": self.config.htf_filter_enabled,
                "htf_conflict_penalty": self.config.htf_conflict_penalty,
                "htf_alignment_boost": self.config.htf_alignment_boost,
                "max_spread_to_stop_ratio": self.config.max_spread_to_stop_ratio,
                "risk_percent": self.config.risk_percent,
                "sl_atr_multiplier": self.config.sl_atr_multiplier,
                "tp_atr_multiplier": self.config.tp_atr_multiplier,
            },
            "recent_executions": self._execution_log[-10:],
        }
