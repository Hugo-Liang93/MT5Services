"""
AI鍙嬪ソ鐨勯敊璇唬鐮佸畾涔?
姣忎釜閿欒浠ｇ爜閮藉寘鍚獳I鍙悊瑙ｇ殑淇℃伅鍜屽缓璁姩浣?
"""

from enum import Enum


class AIErrorCode(str, Enum):
    """AI鍙瘑鍒殑閿欒浠ｇ爜"""
    
    # MT5杩炴帴鐩稿叧閿欒
    MT5_CONNECTION_FAILED = "MT5_CONNECTION_FAILED"
    MT5_SYMBOL_NOT_FOUND = "MT5_SYMBOL_NOT_FOUND"
    MT5_TIMEOUT = "MT5_TIMEOUT"
    MT5_NOT_INITIALIZED = "MT5_NOT_INITIALIZED"
    MT5_LOGIN_FAILED = "MT5_LOGIN_FAILED"
    
    # 鏁版嵁鐩稿叧閿欒
    DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"
    DATA_STALE = "DATA_STALE"
    DATA_CACHE_EMPTY = "DATA_CACHE_EMPTY"
    INVALID_TIMEFRAME = "INVALID_TIMEFRAME"
    INVALID_SYMBOL = "INVALID_SYMBOL"
    
    # 浜ゆ槗鐩稿叧閿欒
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    INVALID_VOLUME = "INVALID_VOLUME"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_STOP_LEVELS = "INVALID_STOP_LEVELS"
    TRADE_EXECUTION_FAILED = "TRADE_EXECUTION_FAILED"
    TRADE_MODIFICATION_FAILED = "TRADE_MODIFICATION_FAILED"
    TRADE_CLOSE_FAILED = "TRADE_CLOSE_FAILED"
    TRADE_CANCEL_FAILED = "TRADE_CANCEL_FAILED"
    INVALID_TRADE_SIDE = "INVALID_TRADE_SIDE"
    TRADE_SIZE_TOO_SMALL = "TRADE_SIZE_TOO_SMALL"
    TRADE_SIZE_TOO_LARGE = "TRADE_SIZE_TOO_LARGE"
    MARKET_CLOSED = "MARKET_CLOSED"
    TRADE_DISABLED = "TRADE_DISABLED"
    TRADE_LIMIT_EXCEEDED = "TRADE_LIMIT_EXCEEDED"
    TRADE_BLOCKED_BY_RISK = "TRADE_BLOCKED_BY_RISK"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MARGIN_INSUFFICIENT_PRE = "MARGIN_INSUFFICIENT_PRE"
    TRADE_FREQUENCY_LIMITED = "TRADE_FREQUENCY_LIMITED"
    POSITION_LIMIT_REACHED = "POSITION_LIMIT_REACHED"
    SESSION_WINDOW_BLOCKED = "SESSION_WINDOW_BLOCKED"
    INVALID_REQUEST = "INVALID_REQUEST"

    ACCOUNT_INFO_FAILED = "ACCOUNT_INFO_FAILED"
    
    # 璐︽埛鐩稿叧閿欒
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    ACCOUNT_READ_ONLY = "ACCOUNT_READ_ONLY"
    ACCOUNT_LIMIT_REACHED = "ACCOUNT_LIMIT_REACHED"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    ACCOUNT_NO_PERMISSION = "ACCOUNT_NO_PERMISSION"
    
    # 鎸囨爣璁＄畻鐩稿叧閿欒
    INDICATOR_CALCULATION_FAILED = "INDICATOR_CALCULATION_FAILED"
    INSUFFICIENT_HISTORY_DATA = "INSUFFICIENT_HISTORY_DATA"
    INVALID_INDICATOR_PARAMS = "INVALID_INDICATOR_PARAMS"
    
    # 绯荤粺閿欒
    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    
    # 缃戠粶閿欒
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    
    # 楠岃瘉閿欒
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_PARAMETER_VALUE = "INVALID_PARAMETER_VALUE"
    UNAUTHORIZED_ACCESS = "UNAUTHORIZED_ACCESS"


class AIErrorAction(str, Enum):
    """AI鍙墽琛岀殑鍔ㄤ綔寤鸿"""
    
    # 閲嶈瘯鐩稿叧
    RETRY_AFTER_DELAY = "retry_after_delay"
    RETRY_IMMEDIATELY = "retry_immediately"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    
    # 妫€鏌ョ浉鍏?
    CHECK_CONNECTION = "check_connection"
    CHECK_CREDENTIALS = "check_credentials"
    CHECK_CONFIGURATION = "check_configuration"
    CHECK_PERMISSIONS = "check_permissions"
    CHECK_RESOURCES = "check_resources"
    CHECK_ACCOUNT_STATUS = "check_account_status"
    CHECK_MARKET_STATUS = "check_market_status"
    
    # 璋冩暣鍙傛暟
    REDUCE_VOLUME = "reduce_volume"
    ADJUST_PRICE = "adjust_price"
    MODIFY_STOP_LEVELS = "modify_stop_levels"
    USE_DIFFERENT_SYMBOL = "use_different_symbol"
    USE_MARKET_ORDER = "use_market_order"
    USE_LIMIT_ORDER = "use_limit_order"
    
    # 绛夊緟鐩稿叧
    WAIT_FOR_DATA = "wait_for_data"
    WAIT_FOR_CONNECTION = "wait_for_connection"
    WAIT_FOR_MARKET_OPEN = "wait_for_market_open"
    WAIT_FOR_ACCOUNT_UPDATE = "wait_for_account_update"
    WAIT_FOR_RISK_WINDOW = "wait_for_risk_window"
    
    # 鏁版嵁鐩稿叧
    USE_FALLBACK_DATA = "use_fallback_data"
    USE_CACHED_DATA = "use_cached_data"
    USE_HISTORICAL_DATA = "use_historical_data"
    
    # 绯荤粺鐩稿叧
    RESTART_SERVICE = "restart_service"
    RELOAD_CONFIGURATION = "reload_configuration"
    CONTACT_SUPPORT = "contact_support"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    
    # 浜ゆ槗鐩稿叧
    CANCEL_ORDER = "cancel_order"
    CLOSE_POSITION = "close_position"
    HEDGE_POSITION = "hedge_position"
    MONITOR_MARKET = "monitor_market"
    MODIFY_POSITION = "modify_position"
    PARTIAL_CLOSE = "partial_close"
    
    # 璐︽埛鐩稿叧
    DEPOSIT_FUNDS = "deposit_funds"
    REDUCE_LEVERAGE = "reduce_leverage"
    CLOSE_SOME_POSITIONS = "close_some_positions"
    SWITCH_ACCOUNT = "switch_account"
    
    # 楠岃瘉鐩稿叧
    VALIDATE_PARAMETERS = "validate_parameters"
    REVIEW_PARAMETERS = "review_parameters"
    CHECK_AUTHORIZATION = "check_authorization"
    UPDATE_CREDENTIALS = "update_credentials"


# 閿欒浠ｇ爜鍒板缓璁姩浣滅殑鏄犲皠
ERROR_ACTION_MAPPING = {
    # MT5杩炴帴鐩稿叧
    AIErrorCode.MT5_CONNECTION_FAILED: AIErrorAction.CHECK_CONNECTION,
    AIErrorCode.MT5_TIMEOUT: AIErrorAction.RETRY_AFTER_DELAY,
    AIErrorCode.MT5_NOT_INITIALIZED: AIErrorAction.RESTART_SERVICE,
    AIErrorCode.MT5_LOGIN_FAILED: AIErrorAction.CHECK_CREDENTIALS,
    
    # 鏁版嵁鐩稿叧
    AIErrorCode.DATA_NOT_AVAILABLE: AIErrorAction.USE_FALLBACK_DATA,
    AIErrorCode.DATA_STALE: AIErrorAction.WAIT_FOR_DATA,
    AIErrorCode.DATA_CACHE_EMPTY: AIErrorAction.WAIT_FOR_DATA,
    
    # 浜ゆ槗鐩稿叧
    AIErrorCode.INSUFFICIENT_MARGIN: AIErrorAction.REDUCE_VOLUME,
    AIErrorCode.INVALID_VOLUME: AIErrorAction.ADJUST_PRICE,
    AIErrorCode.ORDER_REJECTED: AIErrorAction.ADJUST_PRICE,
    AIErrorCode.POSITION_NOT_FOUND: AIErrorAction.CHECK_ACCOUNT_STATUS,
    AIErrorCode.ORDER_NOT_FOUND: AIErrorAction.CHECK_ACCOUNT_STATUS,
    AIErrorCode.INVALID_PRICE: AIErrorAction.USE_MARKET_ORDER,
    AIErrorCode.INVALID_STOP_LEVELS: AIErrorAction.MODIFY_STOP_LEVELS,
    AIErrorCode.TRADE_EXECUTION_FAILED: AIErrorAction.RETRY_AFTER_DELAY,
    AIErrorCode.TRADE_MODIFICATION_FAILED: AIErrorAction.MODIFY_POSITION,
    AIErrorCode.TRADE_CLOSE_FAILED: AIErrorAction.CLOSE_POSITION,
    AIErrorCode.TRADE_CANCEL_FAILED: AIErrorAction.CANCEL_ORDER,
    AIErrorCode.INVALID_TRADE_SIDE: AIErrorAction.VALIDATE_PARAMETERS,
    AIErrorCode.TRADE_SIZE_TOO_SMALL: AIErrorAction.ADJUST_PRICE,
    AIErrorCode.TRADE_SIZE_TOO_LARGE: AIErrorAction.REDUCE_VOLUME,
    AIErrorCode.MARKET_CLOSED: AIErrorAction.WAIT_FOR_MARKET_OPEN,
    AIErrorCode.TRADE_DISABLED: AIErrorAction.CHECK_PERMISSIONS,
    AIErrorCode.TRADE_LIMIT_EXCEEDED: AIErrorAction.CLOSE_SOME_POSITIONS,
    AIErrorCode.TRADE_BLOCKED_BY_RISK: AIErrorAction.WAIT_FOR_RISK_WINDOW,
    AIErrorCode.DAILY_LOSS_LIMIT: AIErrorAction.WAIT_FOR_RISK_WINDOW,
    AIErrorCode.MARGIN_INSUFFICIENT_PRE: AIErrorAction.REDUCE_VOLUME,
    AIErrorCode.TRADE_FREQUENCY_LIMITED: AIErrorAction.WAIT_FOR_RISK_WINDOW,
    AIErrorCode.POSITION_LIMIT_REACHED: AIErrorAction.CLOSE_SOME_POSITIONS,
    AIErrorCode.SESSION_WINDOW_BLOCKED: AIErrorAction.WAIT_FOR_MARKET_OPEN,
    
    # 璐︽埛鐩稿叧
    AIErrorCode.ACCOUNT_NOT_FOUND: AIErrorAction.CHECK_ACCOUNT_STATUS,
    AIErrorCode.ACCOUNT_DISABLED: AIErrorAction.SWITCH_ACCOUNT,
    AIErrorCode.INSUFFICIENT_FUNDS: AIErrorAction.DEPOSIT_FUNDS,
    AIErrorCode.ACCOUNT_READ_ONLY: AIErrorAction.CHECK_PERMISSIONS,
    AIErrorCode.ACCOUNT_LIMIT_REACHED: AIErrorAction.CLOSE_SOME_POSITIONS,
    AIErrorCode.ACCOUNT_SUSPENDED: AIErrorAction.CONTACT_SUPPORT,
    AIErrorCode.ACCOUNT_NO_PERMISSION: AIErrorAction.CHECK_AUTHORIZATION,
    
    # 绯荤粺閿欒
    AIErrorCode.SERVICE_UNAVAILABLE: AIErrorAction.RETRY_AFTER_DELAY,
    AIErrorCode.RATE_LIMIT_EXCEEDED: AIErrorAction.WAIT_FOR_DATA,
    AIErrorCode.INTERNAL_SERVER_ERROR: AIErrorAction.CONTACT_SUPPORT,
    AIErrorCode.DATABASE_ERROR: AIErrorAction.RESTART_SERVICE,
    AIErrorCode.CONFIGURATION_ERROR: AIErrorAction.RELOAD_CONFIGURATION,
    
    # 缃戠粶閿欒
    AIErrorCode.NETWORK_ERROR: AIErrorAction.CHECK_CONNECTION,
    AIErrorCode.TIMEOUT_ERROR: AIErrorAction.RETRY_AFTER_DELAY,
    AIErrorCode.CONNECTION_REFUSED: AIErrorAction.CHECK_CONNECTION,
    
    # 楠岃瘉閿欒
    AIErrorCode.VALIDATION_ERROR: AIErrorAction.VALIDATE_PARAMETERS,
    AIErrorCode.MISSING_REQUIRED_FIELD: AIErrorAction.VALIDATE_PARAMETERS,
    AIErrorCode.INVALID_PARAMETER_VALUE: AIErrorAction.VALIDATE_PARAMETERS,
    AIErrorCode.UNAUTHORIZED_ACCESS: AIErrorAction.CHECK_AUTHORIZATION,
    AIErrorCode.INVALID_REQUEST: AIErrorAction.REVIEW_PARAMETERS,
}


def get_suggested_action(error_code: AIErrorCode) -> str:
    """鏍规嵁閿欒浠ｇ爜鑾峰彇寤鸿鍔ㄤ綔"""
    return ERROR_ACTION_MAPPING.get(error_code, AIErrorAction.CONTACT_SUPPORT)


# 浜ゆ槗鐩稿叧杈呭姪鍑芥暟
def get_trade_error_details(symbol: str, volume: float, side: str, price: float = None) -> dict:
    """鑾峰彇浜ゆ槗閿欒璇︽儏"""
    details = {
        "symbol": symbol,
        "volume": volume,
        "side": side,
        "price": price
    }
    return {k: v for k, v in details.items() if v is not None}


def get_account_error_details(account_id: int = None, operation: str = None, symbol: str = None) -> dict:
    """鑾峰彇璐︽埛閿欒璇︽儏"""
    details = {
        "account_id": account_id,
        "operation": operation,
        "symbol": symbol
    }
    return {k: v for k, v in details.items() if v is not None}

