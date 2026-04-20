"""
Timescale schema and SQL statements grouped by module.

All DDL is aggregated in ``DDL_STATEMENTS`` for centralized initialization.
Individual SQL constants are exported for repository consumption.
"""

from .account_risk_state import DDL as ACCOUNT_RISK_STATE_DDL
from .account_risk_state import UPSERT_SQL as UPSERT_ACCOUNT_RISK_STATE_SQL
from .auto_executions import DDL as AUTO_EXECUTIONS_DDL
from .auto_executions import INSERT_SQL as INSERT_AUTO_EXECUTIONS_SQL

# ── Backtest ─────────────────────────────────────────────────────
from .backtest import DDL as BACKTEST_DDL
from .backtest import INSERT_RUN_SQL as INSERT_BACKTEST_RUN_SQL
from .backtest import INSERT_TRADE_SQL as INSERT_BACKTEST_TRADE_SQL
from .circuit_breaker_history import DDL as CIRCUIT_BREAKER_HISTORY_DDL
from .circuit_breaker_history import INSERT_SQL as INSERT_CIRCUIT_BREAKER_HISTORY_SQL

# ── Economic Calendar ────────────────────────────────────────────
from .economic_calendar import DDL as ECONOMIC_CALENDAR_DDL
from .economic_calendar import (
    DELETE_BY_KEYS_SQL as DELETE_ECONOMIC_CALENDAR_BY_KEYS_SQL,
)
from .economic_calendar import INSERT_UPDATE_SQL as INSERT_ECONOMIC_CALENDAR_UPDATE_SQL
from .economic_calendar import MIGRATION_SQL as ECONOMIC_CALENDAR_MIGRATION_SQL
from .economic_calendar import UPSERT_SQL as UPSERT_ECONOMIC_CALENDAR_SQL
from .execution_intents import DDL as EXECUTION_INTENTS_DDL
from .execution_intents import INSERT_SQL as INSERT_EXECUTION_INTENTS_SQL
from .execution_intents import MIGRATION_SQL as EXECUTION_INTENTS_MIGRATION_SQL
from .intrabar import DDL as INTRABAR_DDL
from .intrabar import INSERT_SQL as INSERT_INTRABAR_SQL
from .market_impact import AGGREGATED_STATS_SQL as MARKET_IMPACT_AGGREGATED_STATS_SQL
from .market_impact import DDL as MARKET_IMPACT_DDL
from .market_impact import FETCH_BY_EVENT_SQL as FETCH_MARKET_IMPACT_BY_EVENT_SQL
from .market_impact import UPSERT_SQL as UPSERT_MARKET_IMPACT_SQL
from .ohlc import DDL as OHLC_DDL
from .ohlc import INSERT_SQL as INSERT_OHLC_SQL
from .ohlc import UPSERT_SQL as UPSERT_OHLC_SQL
from .operator_commands import DDL as OPERATOR_COMMANDS_DDL
from .operator_commands import INSERT_SQL as INSERT_OPERATOR_COMMANDS_SQL
from .operator_commands import MIGRATION_SQL as OPERATOR_COMMANDS_MIGRATION_SQL

# ── Paper Trading ────────────────────────────────────────────────
from .paper_trading import DDL as PAPER_TRADING_DDL
from .pending_order_states import DDL as PENDING_ORDER_STATES_DDL
from .pending_order_states import UPSERT_SQL as UPSERT_PENDING_ORDER_STATES_SQL

# ── Monitoring ───────────────────────────────────────────────────
from .pipeline_trace_events import DDL as PIPELINE_TRACE_EVENTS_DDL
from .pipeline_trace_events import INSERT_SQL as INSERT_PIPELINE_TRACE_EVENTS_SQL
from .position_runtime_states import DDL as POSITION_RUNTIME_STATES_DDL
from .position_runtime_states import UPSERT_SQL as UPSERT_POSITION_RUNTIME_STATES_SQL
from .position_sl_tp_history import DDL as POSITION_SL_TP_HISTORY_DDL
from .position_sl_tp_history import INSERT_SQL as INSERT_POSITION_SL_TP_HISTORY_SQL
from .quotes import DDL as QUOTES_DDL
from .quotes import INSERT_SQL as INSERT_QUOTES_SQL
from .recommendation import DDL as RECOMMENDATION_DDL
from .runtime_tasks import DDL as RUNTIME_TASKS_DDL
from .runtime_tasks import MIGRATION_SQL as RUNTIME_TASKS_MIGRATION_SQL
from .runtime_tasks import UPSERT_SQL as UPSERT_RUNTIME_TASK_STATUS_SQL
from .signal_outcomes import DDL as SIGNAL_OUTCOMES_DDL
from .signal_outcomes import EXPECTANCY_SQL as SIGNAL_OUTCOMES_EXPECTANCY_SQL
from .signal_outcomes import INSERT_SQL as INSERT_SIGNAL_OUTCOMES_SQL
from .signal_outcomes import WINRATE_SQL as SIGNAL_OUTCOMES_WINRATE_SQL

# ── Signal System ────────────────────────────────────────────────
from .signals import DDL as SIGNAL_EVENTS_DDL
from .signals import INSERT_SQL as INSERT_SIGNAL_EVENTS_SQL
from .signals import MIGRATION_SQL as SIGNAL_EVENTS_MIGRATION_SQL
from .signals import PREVIEW_INSERT_SQL as INSERT_SIGNAL_PREVIEW_EVENTS_SQL
from .signals import UPDATE_ADMISSION_SQL as UPDATE_SIGNAL_ADMISSION_SQL

# ── Market Data ──────────────────────────────────────────────────
from .ticks import DDL as TICKS_DDL
from .ticks import INSERT_SQL as INSERT_TICKS_SQL

# ── Trading State ────────────────────────────────────────────────
from .trade_command_audits import DDL as TRADE_COMMAND_AUDITS_DDL
from .trade_command_audits import INSERT_SQL as INSERT_TRADE_COMMAND_AUDITS_SQL
from .trade_control_state import DDL as TRADE_CONTROL_STATE_DDL
from .trade_control_state import UPSERT_SQL as UPSERT_TRADE_CONTROL_STATE_SQL
from .trade_outcomes import DDL as TRADE_OUTCOMES_DDL
from .trade_outcomes import INSERT_SQL as INSERT_TRADE_OUTCOMES_SQL

# ── Centralized DDL execution order ─────────────────────────────
# Tables with foreign keys must come after their referenced tables.
DDL_STATEMENTS = [
    # Market data (hypertables)
    TICKS_DDL,
    QUOTES_DDL,
    OHLC_DDL,
    INTRABAR_DDL,
    # Economic calendar (hypertables)
    ECONOMIC_CALENDAR_DDL,
    MARKET_IMPACT_DDL,
    # Signal system (hypertables)
    SIGNAL_EVENTS_DDL,
    SIGNAL_OUTCOMES_DDL,
    AUTO_EXECUTIONS_DDL,
    EXECUTION_INTENTS_DDL,
    TRADE_OUTCOMES_DDL,
    # Trading state
    TRADE_COMMAND_AUDITS_DDL,
    OPERATOR_COMMANDS_DDL,
    PENDING_ORDER_STATES_DDL,
    POSITION_RUNTIME_STATES_DDL,
    POSITION_SL_TP_HISTORY_DDL,
    TRADE_CONTROL_STATE_DDL,
    CIRCUIT_BREAKER_HISTORY_DDL,
    ACCOUNT_RISK_STATE_DDL,
    # Monitoring
    PIPELINE_TRACE_EVENTS_DDL,
    RUNTIME_TASKS_DDL,
    # Backtest (backtest_trades FK → backtest_runs)
    BACKTEST_DDL,
    RECOMMENDATION_DDL,
    # Paper trading (paper_trade_outcomes FK → paper_trading_sessions)
    PAPER_TRADING_DDL,
]

POST_INIT_DDL_STATEMENTS = [
    ECONOMIC_CALENDAR_MIGRATION_SQL,
    EXECUTION_INTENTS_MIGRATION_SQL,
    OPERATOR_COMMANDS_MIGRATION_SQL,
    RUNTIME_TASKS_MIGRATION_SQL,
    SIGNAL_EVENTS_MIGRATION_SQL,
]

__all__ = [
    "DDL_STATEMENTS",
    "POST_INIT_DDL_STATEMENTS",
    # Market data
    "INSERT_TICKS_SQL",
    "INSERT_QUOTES_SQL",
    "INSERT_OHLC_SQL",
    "UPSERT_OHLC_SQL",
    "INSERT_INTRABAR_SQL",
    # Economic calendar
    "UPSERT_ECONOMIC_CALENDAR_SQL",
    "INSERT_ECONOMIC_CALENDAR_UPDATE_SQL",
    "DELETE_ECONOMIC_CALENDAR_BY_KEYS_SQL",
    "UPSERT_MARKET_IMPACT_SQL",
    "FETCH_MARKET_IMPACT_BY_EVENT_SQL",
    "MARKET_IMPACT_AGGREGATED_STATS_SQL",
    # Signal system
    "INSERT_SIGNAL_EVENTS_SQL",
    "INSERT_SIGNAL_PREVIEW_EVENTS_SQL",
    "UPDATE_SIGNAL_ADMISSION_SQL",
    "INSERT_SIGNAL_OUTCOMES_SQL",
    "SIGNAL_OUTCOMES_EXPECTANCY_SQL",
    "SIGNAL_OUTCOMES_WINRATE_SQL",
    "INSERT_AUTO_EXECUTIONS_SQL",
    "INSERT_EXECUTION_INTENTS_SQL",
    "INSERT_TRADE_OUTCOMES_SQL",
    # Trading state
    "INSERT_TRADE_COMMAND_AUDITS_SQL",
    "INSERT_OPERATOR_COMMANDS_SQL",
    "UPSERT_PENDING_ORDER_STATES_SQL",
    "UPSERT_POSITION_RUNTIME_STATES_SQL",
    "INSERT_POSITION_SL_TP_HISTORY_SQL",
    "UPSERT_TRADE_CONTROL_STATE_SQL",
    "INSERT_CIRCUIT_BREAKER_HISTORY_SQL",
    "UPSERT_ACCOUNT_RISK_STATE_SQL",
    # Monitoring
    "INSERT_PIPELINE_TRACE_EVENTS_SQL",
    "UPSERT_RUNTIME_TASK_STATUS_SQL",
]
