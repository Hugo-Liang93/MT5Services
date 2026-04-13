"""
交易业务层：封装下单/平仓，校验参数。
"""

from __future__ import annotations

import time
from uuid import uuid4
from typing import Any, Dict, Optional

from src.clients.mt5_account import MT5AccountClient
from src.clients.mt5_trading import MT5TradingClient, MT5TradingClientError
from src.risk.service import PreTradeRiskBlockedError, PreTradeRiskService
from src.signals.metadata_keys import MetadataKey as MK
from src.trading.broker.comment_codec import (
    build_trade_comment,
    comment_matches_request_id,
    comments_share_request_tag,
)
from src.trading.models import TradeExecutionDetails
from src.trading.reasons import REASON_XAUUSD_TRADE_GUARD_BLOCKED

from .results import (
    build_blocked_trade_precheck_result,
    build_disabled_trade_precheck_result,
    build_trade_execution_result,
    build_trade_precheck_result,
)


class TradingService:
    def __init__(
        self,
        client: Optional[MT5TradingClient] = None,
        account_client: Optional[MT5AccountClient] = None,
        pre_trade_risk_service: Optional[PreTradeRiskService] = None,
    ):
        self.client = client or MT5TradingClient()
        self.account_client = (
            account_client
            if account_client is not None
            else (MT5AccountClient() if client is None else None)
        )
        self.pre_trade_risk_service = pre_trade_risk_service

    @staticmethod
    def _is_gold_symbol(symbol: str) -> bool:
        normalized = str(symbol or "").strip().upper()
        return normalized.startswith("XAUUSD")

    @staticmethod
    def _build_mt5_comment(
        *,
        comment: str,
        request_id: str,
        metadata: Optional[Dict[str, Any]],
        side: str,
        order_kind: str,
    ) -> str:
        signal_meta = {}
        if isinstance(metadata, dict):
            signal_meta = dict(metadata.get(MK.SIGNAL) or {})
        return build_trade_comment(
            request_id=request_id,
            timeframe=str(signal_meta.get("timeframe") or ""),
            strategy=str(signal_meta.get("strategy") or ""),
            side=side,
            order_kind=order_kind,
            comment=comment,
        )

    def health(self) -> dict[str, Any]:
        """暴露交易服务运行状态，供应用层统一读取。

        不再通过外部访问 client 属性后再反射调用，职责收敛到服务内部端口。
        """
        try:
            status = self.client.health()
            if isinstance(status, dict):
                return status
            return {"status": str(status), "connected": True}
        except Exception as exc:
            return {"status": "failed", "connected": False, "error": str(exc)}

    def _recover_trade_from_state(
        self,
        *,
        symbol: str,
        submitted_comment: str,
        request_id: str,
    ) -> Optional[TradeExecutionDetails]:
        target_comment = str(submitted_comment or "").strip()
        if self.account_client is None or (not target_comment and not request_id):
            return None

        for position in self.get_positions(symbol=symbol):
            position_comment = str(position.comment or "").strip()
            matches_comment = bool(
                target_comment
                and (
                    position_comment == target_comment
                    or comments_share_request_tag(position_comment, target_comment)
                )
            )
            matches_request = bool(
                request_id and comment_matches_request_id(position_comment, request_id)
            )
            if not matches_comment and not matches_request:
                continue
            return TradeExecutionDetails(
                ticket=int(position.ticket or 0),
                order_id=0,
                deal_id=0,
                symbol=symbol,
                volume=float(position.volume or 0.0),
                requested_price=None,
                fill_price=float(position.price_open or 0.0),
                recovered_from_state=True,
                state_source="positions",
            )
        for order in self.get_orders(symbol=symbol):
            order_comment = str(order.comment or "").strip()
            matches_comment = bool(
                target_comment
                and (
                    order_comment == target_comment
                    or comments_share_request_tag(order_comment, target_comment)
                )
            )
            matches_request = bool(
                request_id and comment_matches_request_id(order_comment, request_id)
            )
            if not matches_comment and not matches_request:
                continue
            return TradeExecutionDetails(
                ticket=int(order.ticket or 0),
                order_id=int(order.ticket or 0),
                deal_id=0,
                symbol=symbol,
                volume=float(order.volume or 0.0),
                requested_price=float(order.price_open or 0.0),
                fill_price=float(order.price_open or 0.0),
                pending=True,
                recovered_from_state=True,
                state_source="orders",
            )
        return None

    @staticmethod
    def _coerce_trade_guard_block(
        assessment: Dict[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        updated = dict(assessment or {})
        updated["verdict"] = "block"
        updated["blocked"] = True
        updated["reason"] = reason
        warnings = list(updated.get("warnings") or [])
        if reason not in warnings:
            warnings.append(reason)
        updated["warnings"] = warnings
        return updated

    def open(
        self,
        symbol: str,
        volume: float,
        side: str,
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
    ) -> int:
        order_type = self._side_to_order_type(side, order_kind=order_kind)
        return self.client.open_trade(
            symbol=symbol,
            volume=volume,
            order_type=order_type,
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=comment,
            magic=magic,
        )

    def close(
        self,
        ticket: int,
        deviation: int = 20,
        comment: str = "",
        volume: Optional[float] = None,
    ) -> bool:
        return self.client.close_position(ticket, deviation=deviation, comment=comment, volume=volume)

    def close_all(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        side: Optional[str] = None,
        deviation: int = 20,
        comment: str = "close_all",
    ) -> dict:
        return self.client.close_positions(
            symbol=symbol,
            magic=magic,
            side=side,
            deviation=deviation,
            comment=comment,
        )

    def cancel_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None) -> dict:
        return self.client.cancel_orders(symbol=symbol, magic=magic)

    def estimate_margin(self, symbol: str, volume: float, side: str, price: Optional[float] = None) -> float:
        return self.client.estimate_margin(symbol=symbol, volume=volume, side=side, price=price)

    def modify_orders(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        return self.client.modify_orders(symbol=symbol, magic=magic, sl=sl, tp=tp)

    def modify_positions(
        self,
        ticket: Optional[int] = None,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> dict:
        return self.client.modify_positions(ticket=ticket, symbol=symbol, magic=magic, sl=sl, tp=tp)

    def _side_to_order_type(self, side: str, order_kind: str = "market") -> int:
        return self.client.side_and_kind_to_order_type(side, order_kind=order_kind)

    # --- 交易执行核心 API ---
    def execute_trade(
        self,
        symbol: str,
        volume: float,
        side: str,
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
        dry_run: bool = False,
        retry_attempts: int = 2,
        retry_backoff_ms: int = 120,
        request_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict:
        request_id = request_id or uuid4().hex
        risk_assessment: Optional[Dict[str, Any]] = None
        submitted_comment = self._build_mt5_comment(
            comment=comment,
            request_id=request_id,
            metadata=metadata,
            side=side,
            order_kind=order_kind,
        )
        # 先估算保证金，注入 metadata 供 MarginAvailabilityRule 使用
        margin_estimate: Optional[float] = None
        try:
            margin_estimate = self.estimate_margin(symbol=symbol, volume=volume, side=side, price=price)
        except Exception:
            margin_estimate = None
        enriched_metadata = dict(metadata or {})
        if margin_estimate is not None:
            enriched_metadata[MK.ESTIMATED_MARGIN] = margin_estimate
        if self.pre_trade_risk_service is not None:
            risk_assessment = self.pre_trade_risk_service.enforce_trade_allowed(
                symbol=symbol,
                volume=volume,
                side=side,
                order_kind=order_kind,
                price=price,
                sl=sl,
                tp=tp,
                deviation=deviation,
                comment=submitted_comment,
                magic=magic,
                metadata=enriched_metadata,
            )

        precheck_snapshot = self.precheck_trade(
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=submitted_comment,
            magic=magic,
            metadata=metadata,
        )
        if self._is_gold_symbol(symbol) and bool(precheck_snapshot.get("event_blocked")):
            blocked_snapshot = self._coerce_trade_guard_block(
                precheck_snapshot,
                reason=REASON_XAUUSD_TRADE_GUARD_BLOCKED,
            )
            raise PreTradeRiskBlockedError(
                blocked_snapshot["reason"],
                assessment=blocked_snapshot,
            )
        if dry_run:
            return build_trade_execution_result(
                {},
                request_id=request_id,
                dry_run=True,
                symbol=symbol,
                volume=volume,
                side=side,
                order_kind=order_kind,
                requested_price=price,
                comment=submitted_comment,
                estimated_margin=margin_estimate,
                pre_trade_risk=risk_assessment,
                precheck=precheck_snapshot,
                execution_attempts=0,
                execution_state="skipped",
            )
        if not bool(precheck_snapshot.get("executable", True)):
            raise MT5TradingClientError(str(precheck_snapshot.get("reason") or "Trade precheck failed"))

        last_error: Optional[Exception] = None
        max_attempts = max(int(retry_attempts), 1)
        result: TradeExecutionDetails | Dict[str, Any] | None = None
        execution_attempts: Optional[int] = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = self.client.open_trade_details(
                    symbol=symbol,
                    volume=volume,
                    order_type=self._side_to_order_type(side, order_kind=order_kind),
                    price=price,
                    sl=sl,
                    tp=tp,
                    deviation=deviation,
                    comment=submitted_comment,
                    magic=magic,
                )
                execution_attempts = attempt
                break
            except Exception as exc:
                recovered = self._recover_trade_from_state(
                    symbol=symbol,
                    submitted_comment=submitted_comment,
                    request_id=request_id,
                )
                if recovered is not None:
                    result = recovered
                    execution_attempts = attempt
                    break
                last_error = exc
                if attempt >= max_attempts:
                    raise
                time.sleep(max(float(retry_backoff_ms), 0.0) / 1000.0)
        if result is None and last_error is not None:
            raise last_error

        # 记录交易频率（供 TradeFrequencyRule 使用）
        if self.pre_trade_risk_service is not None:
            try:
                self.pre_trade_risk_service.record_trade_execution()
            except Exception:
                pass
        self._invalidate_account_cache()
        try:
            positions_count = len(self.get_positions(symbol=symbol))
        except Exception:
            positions_count = None
        try:
            orders_count = len(self.get_orders(symbol=symbol))
        except Exception:
            orders_count = None
        state_consistency = {
            "positions_count": positions_count,
            "orders_count": orders_count,
        }

        return build_trade_execution_result(
            result,
            request_id=request_id,
            dry_run=False,
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
            requested_price=price,
            comment=submitted_comment,
            estimated_margin=margin_estimate,
            pre_trade_risk=risk_assessment,
            precheck=precheck_snapshot,
            state_consistency=state_consistency,
            execution_attempts=execution_attempts,
        )

    def precheck_trade(
        self,
        symbol: str,
        volume: Optional[float] = None,
        side: Optional[str] = None,
        order_kind: str = "market",
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        deviation: int = 20,
        comment: str = "",
        magic: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request_id = uuid4().hex
        checks: list[dict[str, Any]] = []
        warnings: list[str] = []
        if volume is not None and volume <= 0:
            checks.append({"name": "volume_positive", "passed": False, "message": "volume must be > 0"})
            return build_blocked_trade_precheck_result(
                symbol=symbol,
                request_id=request_id,
                reason="volume must be > 0",
                checks=checks,
                warnings=warnings,
                suggested_adjustment={"volume": 0.01},
            )
        # ── Broker 约束检查（独立于 validate_trade_request，提供结构化结果）──
        check_broker_constraints = self.client.check_broker_constraints
        if side:
            try:
                broker_checks = check_broker_constraints(
                    symbol=symbol,
                    side=side,
                    request_price=price,
                    sl=sl,
                    tp=tp,
                )
                checks.extend(broker_checks)
                broker_failures = [c for c in broker_checks if not c["passed"]]
                if broker_failures:
                    reason = "; ".join(c["message"] for c in broker_failures)
                    return build_blocked_trade_precheck_result(
                        symbol=symbol,
                        request_id=request_id,
                        reason=reason,
                        checks=checks,
                        warnings=warnings,
                        suggested_adjustment={"verdict": "adjust_sl_tp_or_wait"},
                    )
                # freeze_level 提示作为 warning
                for c in broker_checks:
                    if c["name"] == "freeze_level":
                        warnings.append(c["message"])
            except Exception as exc:
                warnings.append(f"Broker constraint check unavailable: {exc}")
        validate_trade_request = self.client.validate_trade_request
        if volume is not None and side:
            try:
                validate_trade_request(
                    symbol=symbol,
                    volume=volume,
                    side=side,
                    order_kind=order_kind,
                    price=price,
                    sl=sl,
                    tp=tp,
                )
            except Exception as exc:
                checks.append({"name": "trade_parameters", "passed": False, "message": str(exc)})
                return build_blocked_trade_precheck_result(
                    symbol=symbol,
                    request_id=request_id,
                    reason=str(exc),
                    checks=checks,
                    warnings=warnings,
                    suggested_adjustment={"verdict": "review_trade_parameters"},
                )
        if self.pre_trade_risk_service is None:
            return build_disabled_trade_precheck_result(
                symbol=symbol,
                request_id=request_id,
            )
        # 预计算保证金，注入 metadata 供 MarginAvailabilityRule 使用
        # （与 execute_trade() 保持一致的风控链路）
        margin_estimate: Optional[float] = None
        if volume is not None and side:
            try:
                margin_estimate = self.estimate_margin(
                    symbol=symbol, volume=volume, side=side, price=price,
                )
            except Exception:
                margin_estimate = None
        enriched_metadata = dict(metadata or {})
        if margin_estimate is not None:
            enriched_metadata[MK.ESTIMATED_MARGIN] = margin_estimate

        assessment = self.pre_trade_risk_service.assess_trade(
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
            price=price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            comment=comment,
            magic=magic,
            metadata=enriched_metadata,
        )
        return build_trade_precheck_result(
            assessment,
            request_id=request_id,
            checks=checks,
            warnings=warnings,
            estimated_margin=margin_estimate,
        )

    def get_position_close_details(
        self,
        ticket: int,
        *,
        symbol: Optional[str] = None,
        lookback_days: int = 7,
    ) -> Optional[Dict[str, Any]]:
        return self.client.get_position_close_details(
            ticket=ticket,
            symbol=symbol,
            lookback_days=lookback_days,
        )

    def close_position(
        self,
        ticket: int,
        deviation: int = 20,
        comment: str = "",
        volume: Optional[float] = None,
    ) -> dict:
        success = self.close(ticket=ticket, deviation=deviation, comment=comment, volume=volume)
        self._invalidate_account_cache()
        return {"ticket": ticket, "success": success, "volume": volume}

    def close_all_positions(
        self,
        symbol: Optional[str] = None,
        magic: Optional[int] = None,
        side: Optional[str] = None,
        deviation: int = 20,
        comment: str = "close_all",
    ) -> dict:
        return self.close_all(
            symbol=symbol,
            magic=magic,
            side=side,
            deviation=deviation,
            comment=comment,
        )

    def _invalidate_account_cache(self) -> None:
        """下单/平仓后使账户/持仓短 TTL 缓存失效。"""
        if self.account_client is None:
            return
        self.account_client.invalidate_cache()

    def get_positions(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        if self.account_client is None:
            return []
        positions = self.account_client.positions(symbol=symbol)
        if magic is not None:
            positions = [p for p in positions if p.magic == magic]
        return positions

    def get_orders(self, symbol: Optional[str] = None, magic: Optional[int] = None):
        if self.account_client is None:
            return []
        orders = self.account_client.orders(symbol=symbol)
        if magic is not None:
            orders = [o for o in orders if o.magic == magic]
        return orders

    def execute_trade_batch(self, trades: list[dict], stop_on_error: bool = False) -> dict:
        results = []
        success_count = 0
        failure_count = 0
        for index, trade in enumerate(trades):
            try:
                result = self.execute_trade(**trade)
                results.append({"index": index, "success": True, "result": result})
                success_count += 1
            except Exception as exc:
                results.append({"index": index, "success": False, "error": str(exc), "trade": dict(trade)})
                failure_count += 1
                if stop_on_error:
                    break
        return {
            "results": results,
            "success_count": success_count,
            "failure_count": failure_count,
            "stop_on_error": stop_on_error,
        }

    def close_positions_by_tickets(
        self,
        tickets: list[int],
        deviation: int = 20,
        comment: str = "close_batch",
    ) -> dict:
        return self.client.close_positions_by_tickets(
            tickets=tickets,
            deviation=deviation,
            comment=comment,
        )

    def cancel_orders_by_tickets(self, tickets: list[int]) -> dict:
        return self.client.cancel_orders_by_tickets(tickets)
