from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import (
    get_position_manager,
    get_runtime_read_model,
    get_trade_admission_service,
    get_trade_executor,
    get_trading_command_service,
)
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import (
    AdmissionReportModel,
    ApiResponse,
    BatchTradeRequest,
    EstimateMarginRequest,
    ModifyOrdersRequest,
    ModifyPositionsRequest,
    TradeDispatchRequest,
    TradeReconcileRequest,
    TradeRequest,
)
from src.api.trade_dispatcher import TradeAPIDispatcher
from src.clients.base import MT5TradeError
from src.readmodels.runtime import RuntimeReadModel
from src.risk.service import PreTradeRiskBlockedError
from src.trading.admission import TradeAdmissionService
from src.trading.application.services import TradingCommandService
from src.trading.execution.executor import TradeExecutor
from src.trading.positions import PositionManager

from ..common import trade_request_details
from .common import (
    build_trade_execution_error_response,
    build_trade_risk_blocked_response,
    build_unexpected_trade_operation_response,
)

router = APIRouter(tags=["trade"])


@router.post("/trade/dispatch", response_model=ApiResponse[dict])
def trade_dispatch(
    request: TradeDispatchRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
    admission_service: TradeAdmissionService = Depends(get_trade_admission_service),
) -> ApiResponse[dict]:
    dispatcher = TradeAPIDispatcher(service, admission_service=admission_service)
    return dispatcher.dispatch(request.operation, request.payload)


@router.post("/trade/reconcile", response_model=ApiResponse[dict])
def trade_reconcile(
    request: TradeReconcileRequest,
    manager: PositionManager = Depends(get_position_manager),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    reconcile_result = (
        manager.sync_open_positions()
        if request.sync_open_positions
        else {"skipped": True}
    )
    return ApiResponse.success_response(
        data={
            "reconcile": reconcile_result,
            "position_manager": runtime_views.position_manager_summary(),
            "tracked_positions": runtime_views.tracked_positions_payload(limit=100),
            "trading_state": runtime_views.trading_state_summary(
                pending_limit=20,
                position_limit=20,
            ),
        },
        metadata={"operation": "trade_reconcile"},
    )


@router.post("/trade/precheck", response_model=ApiResponse[AdmissionReportModel])
def trade_precheck(
    request: TradeRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
    admission_service: TradeAdmissionService = Depends(get_trade_admission_service),
) -> ApiResponse[AdmissionReportModel]:
    active_alias = service.active_account_alias
    try:
        evaluation = admission_service.evaluate_trade_payload(
            request.model_dump(exclude_none=True),
            requested_operation="trade_precheck",
        )
        report = dict(evaluation.get("report") or {})
        return ApiResponse.success_response(
            data=AdmissionReportModel(**report),
            metadata={
                "operation": "trade_precheck",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "side": request.side,
                "volume": request.volume,
                "trace_id": report.get("trace_id"),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_EXECUTION_FAILED,
            error_message=f"Trade precheck failed: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                **trade_request_details(request),
            },
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"Trade precheck failed: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                **trade_request_details(request),
            },
        )


@router.post("/trade", response_model=ApiResponse[dict])
def trade(
    request: TradeRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.execute_trade(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            order_kind=request.order_kind,
            price=request.price,
            sl=request.sl,
            tp=request.tp,
            deviation=request.deviation,
            comment=request.comment,
            magic=request.magic,
            dry_run=request.dry_run,
            request_id=request.request_id,
        )
        if result is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.TRADE_EXECUTION_FAILED,
                error_message="Trade execution failed",
                suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
                details=trade_request_details(request),
            )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "trade_execution",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "side": request.side,
                "order_kind": request.order_kind,
                "volume": request.volume,
                "price": result.get("price") if result else None,
                "ticket": result.get("ticket") if result else None,
                "dry_run": request.dry_run,
                "request_id": (
                    result.get("request_id") if result else request.request_id
                ),
                "trace_id": result.get("trace_id") if result else None,
                "operation_id": result.get("operation_id") if result else None,
            },
        )
    except PreTradeRiskBlockedError as exc:
        return build_trade_risk_blocked_response(
            exc=exc,
            details=trade_request_details(request),
            account_alias=active_alias,
        )
    except MT5TradeError as exc:
        return build_trade_execution_error_response(
            exc=exc,
            details=trade_request_details(request),
            account_alias=active_alias,
        )


@router.post("/trade/batch", response_model=ApiResponse[dict])
def trade_batch(
    request: BatchTradeRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    request_details = {
        "count": len(request.trades),
        "stop_on_error": request.stop_on_error,
    }
    try:
        result = service.execute_trade_batch(
            trades=[trade.model_dump() for trade in request.trades],
            stop_on_error=request.stop_on_error,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "trade_batch",
                "account_alias": active_alias,
                **request_details,
            },
        )
    except MT5TradeError as exc:
        return build_trade_execution_error_response(
            exc=exc,
            details=request_details,
            account_alias=active_alias,
            error_prefix="Trade batch error",
        )
    except Exception as exc:
        return build_unexpected_trade_operation_response(
            operation="trade_batch",
            exc=exc,
            details=request_details,
            account_alias=active_alias,
        )


@router.post("/estimate_margin", response_model=ApiResponse[dict])
def estimate_margin(
    request: EstimateMarginRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    request_details = trade_request_details(
        TradeRequest(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            price=request.price,
        )
    )
    try:
        result = service.estimate_margin(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            price=request.price,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "estimate_margin",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "volume": request.volume,
                "side": request.side,
                "price": request.price,
            },
        )
    except MT5TradeError as exc:
        return build_trade_execution_error_response(
            exc=exc,
            details=request_details,
            account_alias=active_alias,
            default_error_code=AIErrorCode.TRADE_EXECUTION_FAILED,
            default_suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            error_prefix="Margin estimate error",
        )
    except Exception as exc:
        return build_unexpected_trade_operation_response(
            operation="estimate_margin",
            exc=exc,
            details=request_details,
            account_alias=active_alias,
        )


@router.put("/modify_orders", response_model=ApiResponse[dict])
def modify_orders(
    request: ModifyOrdersRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    request_details = {
        "symbol": request.symbol,
        "magic": request.magic,
    }
    try:
        result = service.modify_orders(
            symbol=request.symbol,
            magic=request.magic,
            sl=request.sl,
            tp=request.tp,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "modify_orders",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
                "modified_count": len(result.get("modified", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return build_trade_execution_error_response(
            exc=exc,
            details=request_details,
            account_alias=active_alias,
            default_error_code=AIErrorCode.TRADE_MODIFICATION_FAILED,
            default_suggested_action=AIErrorAction.MODIFY_POSITION,
            error_prefix="Modify orders error",
        )
    except Exception as exc:
        return build_unexpected_trade_operation_response(
            operation="modify_orders",
            exc=exc,
            details=request_details,
            account_alias=active_alias,
            suggested_action=AIErrorAction.MODIFY_POSITION,
        )


@router.post("/trade/circuit/reset", response_model=ApiResponse[dict])
def reset_circuit_breaker(
    executor: TradeExecutor = Depends(get_trade_executor),
) -> ApiResponse[dict]:
    """手动重置交易熔断器，恢复自动交易。"""
    was_open = executor.circuit_open
    executor.reset_circuit(event="manual_reset", reason="operator_api_call")
    return ApiResponse.success_response(
        data={
            "was_open": was_open,
            "circuit_open": executor.circuit_open,
            "consecutive_failures": executor.consecutive_failures,
        },
        metadata={"operation": "circuit_breaker_reset"},
    )


@router.put("/modify_positions", response_model=ApiResponse[dict])
def modify_positions(
    request: ModifyPositionsRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    request_details = {
        "ticket": request.ticket,
        "symbol": request.symbol,
        "magic": request.magic,
    }
    try:
        result = service.modify_positions(
            ticket=request.ticket,
            symbol=request.symbol,
            magic=request.magic,
            sl=request.sl,
            tp=request.tp,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "modify_positions",
                "account_alias": active_alias,
                "ticket": request.ticket,
                "symbol": request.symbol,
                "magic": request.magic,
                "modified_count": len(result.get("modified", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return build_trade_execution_error_response(
            exc=exc,
            details=request_details,
            account_alias=active_alias,
            default_error_code=AIErrorCode.TRADE_MODIFICATION_FAILED,
            default_suggested_action=AIErrorAction.MODIFY_POSITION,
            error_prefix="Modify positions error",
        )
    except Exception as exc:
        return build_unexpected_trade_operation_response(
            operation="modify_positions",
            exc=exc,
            details=request_details,
            account_alias=active_alias,
            suggested_action=AIErrorAction.MODIFY_POSITION,
        )
