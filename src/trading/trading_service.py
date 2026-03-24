"""
交易业务层：封装下单/平仓，校验参数。
"""

from __future__ import annotations

import time
from uuid import uuid4
from typing import Any, Dict, Optional

from src.clients.mt5_trading import MT5TradingClient, MT5TradingClientError
from src.clients.mt5_account import MT5AccountClient
from src.risk.service import PreTradeRiskBlockedError, PreTradeRiskService


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
    def _request_tag(request_id: str) -> str:
        normalized = "".join(ch for ch in str(request_id or "") if ch.isalnum())
        return normalized[:8].lower()

    def _tagged_comment(self, comment: str, request_id: str) -> str:
        tag = self._request_tag(request_id)
        base = str(comment or "").strip() or "trade"
        if not tag:
            return base
        suffix = f"_r{tag}"
        if base.endswith(suffix):
            return base
        trimmed = base[: max(0, 27 - len(suffix))]
        return f"{trimmed}{suffix}"

    def _recover_trade_from_state(
        self,
        *,
        symbol: str,
        side: str,
        tagged_comment: str,
        request_id: str,
    ) -> Optional[Dict[str, Any]]:
        target_comment = str(tagged_comment or "").strip()
        if not target_comment or self.account_client is None:
            return None

        side_value = str(side or "").strip().lower()
        for position in self.get_positions(symbol=symbol):
            if str(getattr(position, "comment", "") or "").strip() != target_comment:
                continue
            return {
                "ticket": int(getattr(position, "ticket", 0) or 0),
                "order": 0,
                "deal": 0,
                "fill_price": float(getattr(position, "price_open", 0.0) or 0.0),
                "price": float(getattr(position, "price_open", 0.0) or 0.0),
                "requested_price": None,
                "symbol": symbol,
                "volume": float(getattr(position, "volume", 0.0) or 0.0),
                "side": side_value,
                "comment": target_comment,
                "request_id": request_id,
                "recovered_from_state": True,
                "state_source": "positions",
            }
        for order in self.get_orders(symbol=symbol):
            if str(getattr(order, "comment", "") or "").strip() != target_comment:
                continue
            return {
                "ticket": int(getattr(order, "ticket", 0) or 0),
                "order": int(getattr(order, "ticket", 0) or 0),
                "deal": 0,
                "fill_price": float(getattr(order, "price_open", 0.0) or 0.0),
                "price": float(getattr(order, "price_open", 0.0) or 0.0),
                "requested_price": float(getattr(order, "price_open", 0.0) or 0.0),
                "symbol": symbol,
                "volume": float(getattr(order, "volume", 0.0) or 0.0),
                "side": side_value,
                "comment": target_comment,
                "request_id": request_id,
                "recovered_from_state": True,
                "state_source": "orders",
                "pending": True,
            }
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

    @staticmethod
    def _blocked_precheck_response(
        *,
        symbol: str,
        request_id: str,
        reason: str,
        checks: list[dict[str, Any]],
        warnings: Optional[list[str]] = None,
        suggested_adjustment: Optional[dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "enabled": True,
            "mode": "strict",
            "blocked": True,
            "verdict": "block",
            "reason": reason,
            "symbol": symbol,
            "active_windows": [],
            "upcoming_windows": [],
            "checks": checks,
            "warnings": warnings or [],
            "estimated_margin": None,
            "margin_error": None,
            "request_id": request_id,
            "executable": False,
            "suggested_adjustment": suggested_adjustment,
        }

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
        if hasattr(self.client, "side_and_kind_to_order_type"):
            return self.client.side_and_kind_to_order_type(side, order_kind)
        if not hasattr(self.client, "connect"):
            raise MT5TradingClientError("Trading client not initialized")
        side_lower = side.lower()
        if order_kind.lower() != "market":
            raise MT5TradingClientError(f"Unsupported order kind without native client support: {order_kind}")
        if side_lower in ("buy", "long"):
            return __import__("MetaTrader5").ORDER_TYPE_BUY
        if side_lower in ("sell", "short"):
            return __import__("MetaTrader5").ORDER_TYPE_SELL
        raise MT5TradingClientError(f"Unsupported side: {side}")

    # --- Backward-compatible API expected by src.api.trade ---
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
        tagged_comment = self._tagged_comment(comment, request_id)
        # 先估算保证金，注入 metadata 供 MarginAvailabilityRule 使用
        margin_estimate: Optional[float] = None
        try:
            margin_estimate = self.estimate_margin(symbol=symbol, volume=volume, side=side, price=price)
        except Exception:
            margin_estimate = None
        enriched_metadata = dict(metadata or {})
        if margin_estimate is not None:
            enriched_metadata["estimated_margin"] = margin_estimate
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
                comment=tagged_comment,
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
            comment=tagged_comment,
            magic=magic,
            metadata=metadata,
        )
        if self._is_gold_symbol(symbol) and bool(precheck_snapshot.get("event_blocked")):
            blocked_snapshot = self._coerce_trade_guard_block(
                precheck_snapshot,
                reason="xauusd_trade_guard_blocked",
            )
            raise PreTradeRiskBlockedError(
                blocked_snapshot["reason"],
                assessment=blocked_snapshot,
            )
        if dry_run:
            return {
                "request_id": request_id,
                "dry_run": True,
                "symbol": symbol,
                "volume": volume,
                "side": side,
                "order_kind": order_kind,
                "requested_price": price,
                "comment": tagged_comment,
                "estimated_margin": margin_estimate,
                "pre_trade_risk": risk_assessment,
                "precheck": precheck_snapshot,
                "execution_attempts": 0,
                "execution_state": "skipped",
            }
        if not bool(precheck_snapshot.get("executable", True)):
            raise MT5TradingClientError(str(precheck_snapshot.get("reason") or "Trade precheck failed"))

        if hasattr(self.client, "open_trade_details"):
            last_error: Optional[Exception] = None
            max_attempts = max(int(retry_attempts), 1)
            result: Dict[str, Any] | None = None
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
                        comment=tagged_comment,
                        magic=magic,
                    )
                    result["execution_attempts"] = attempt
                    break
                except Exception as exc:
                    recovered = self._recover_trade_from_state(
                        symbol=symbol,
                        side=side,
                        tagged_comment=tagged_comment,
                        request_id=request_id,
                    )
                    if recovered is not None:
                        recovered["execution_attempts"] = attempt
                        result = recovered
                        break
                    last_error = exc
                    if attempt >= max_attempts:
                        raise
                    time.sleep(max(float(retry_backoff_ms), 0.0) / 1000.0)
            if result is None and last_error is not None:
                raise last_error
        else:
            ticket = self.open(
                symbol=symbol,
                volume=volume,
                side=side,
                order_kind=order_kind,
                price=price,
                sl=sl,
                tp=tp,
                deviation=deviation,
                comment=tagged_comment,
                magic=magic,
            )
            result = {
                "ticket": ticket,
                "price": price,
                "requested_price": price,
                "fill_price": price,
                "execution_attempts": 1,
                "comment": tagged_comment,
            }

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

        result.update(
            {
                "request_id": request_id,
                "dry_run": False,
                "symbol": symbol,
                "volume": volume,
                "side": side,
                "order_kind": order_kind,
                "requested_price": price,
                "comment": tagged_comment,
                "estimated_margin": margin_estimate,
                "pre_trade_risk": risk_assessment,
                "precheck": precheck_snapshot,
                "state_consistency": state_consistency,
            }
        )
        return result

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
            return self._blocked_precheck_response(
                symbol=symbol,
                request_id=request_id,
                reason="volume must be > 0",
                checks=checks,
                warnings=warnings,
                suggested_adjustment={"volume": 0.01},
            )
        # ── Broker 约束检查（独立于 validate_trade_request，提供结构化结果）──
        if side and hasattr(self.client, "check_broker_constraints"):
            try:
                broker_checks = self.client.check_broker_constraints(
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
                    return self._blocked_precheck_response(
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
        if volume is not None and side and hasattr(self.client, "validate_trade_request"):
            try:
                self.client.validate_trade_request(
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
                return self._blocked_precheck_response(
                    symbol=symbol,
                    request_id=request_id,
                    reason=str(exc),
                    checks=checks,
                    warnings=warnings,
                    suggested_adjustment={"verdict": "review_trade_parameters"},
                )
        if self.pre_trade_risk_service is None:
            return {
                "enabled": False,
                "mode": "off",
                "blocked": False,
                "verdict": "allow",
                "reason": None,
                "symbol": symbol,
                "active_windows": [],
                "upcoming_windows": [],
                "checks": [],
                "warnings": [],
                "request_id": request_id,
                "executable": True,
                "suggested_adjustment": None,
            }
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
            metadata=metadata,
        )
        assessment["estimated_margin"] = None
        assessment.setdefault("warnings", [])
        assessment.setdefault("checks", [])
        # 合并前面收集的 broker 约束检查结果和 warnings
        if checks:
            assessment["checks"] = checks + assessment["checks"]
        if warnings:
            assessment["warnings"] = warnings + assessment["warnings"]
        if volume is not None and side:
            try:
                assessment["estimated_margin"] = self.estimate_margin(
                    symbol=symbol,
                    volume=volume,
                    side=side,
                    price=price,
                )
            except Exception as exc:
                assessment.setdefault("warnings", []).append(f"Margin estimate unavailable: {exc}")
                assessment["margin_error"] = str(exc)
        assessment["request_id"] = request_id
        assessment["executable"] = str(assessment.get("verdict") or "allow").lower() != "block"
        if not assessment["executable"]:
            assessment["suggested_adjustment"] = {"verdict": "review_risk_windows"}
        elif assessment.get("margin_error"):
            assessment["suggested_adjustment"] = {"verdict": "retry_margin_estimate"}
        else:
            assessment["suggested_adjustment"] = None
        return assessment

    def get_position_close_details(
        self,
        ticket: int,
        *,
        symbol: Optional[str] = None,
        lookback_days: int = 7,
    ) -> Optional[Dict[str, Any]]:
        if not hasattr(self.client, "get_position_close_details"):
            return None
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
        if self.account_client is not None and hasattr(self.account_client, "invalidate_cache"):
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
        if hasattr(self.client, "close_positions_by_tickets"):
            return self.client.close_positions_by_tickets(
                tickets=tickets,
                deviation=deviation,
                comment=comment,
            )
        closed, failed = [], []
        for ticket in tickets:
            try:
                self.close(ticket=ticket, deviation=deviation, comment=comment)
                closed.append(ticket)
            except Exception as exc:
                failed.append({"ticket": ticket, "error": str(exc)})
        return {"closed": closed, "failed": failed}

    def cancel_orders_by_tickets(self, tickets: list[int]) -> dict:
        if hasattr(self.client, "cancel_orders_by_tickets"):
            return self.client.cancel_orders_by_tickets(tickets)
        return {"canceled": [], "failed": [{"ticket": int(ticket), "error": "unsupported"} for ticket in tickets]}
