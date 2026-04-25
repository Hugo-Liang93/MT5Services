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

# ── Correlation Analysis (P11 Phase 3) ───────────────────────────
from .correlation import DDL as CORRELATION_DDL
from .correlation import FETCH_BY_ID_SQL as FETCH_CORRELATION_BY_ID_SQL
from .correlation import FETCH_LATEST_BY_RUN_SQL as FETCH_CORRELATION_LATEST_SQL
from .correlation import INSERT_SQL as INSERT_CORRELATION_SQL
from .correlation import LIST_BY_RUN_SQL as LIST_CORRELATION_BY_RUN_SQL

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

# ── Walk-Forward (P11 Phase 2) ───────────────────────────────────
from .walk_forward import DDL as WALK_FORWARD_DDL
from .walk_forward import FETCH_LATEST_BY_BACKTEST_RUN_SQL as FETCH_WF_LATEST_SQL
from .walk_forward import FETCH_RUN_SQL as FETCH_WF_RUN_SQL
from .walk_forward import FETCH_WINDOWS_SQL as FETCH_WF_WINDOWS_SQL
from .walk_forward import INSERT_RUN_SQL as INSERT_WF_RUN_SQL
from .walk_forward import INSERT_WINDOW_SQL as INSERT_WF_WINDOW_SQL
from .walk_forward import LIST_RUNS_SQL as LIST_WF_RUNS_SQL

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
    # Walk-Forward 依赖 backtest_runs（外键），必须在其后
    WALK_FORWARD_DDL,
    # Correlation 分析结果（不设 FK，允许独立历史保留）
    CORRELATION_DDL,
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
    # Walk-Forward
    "INSERT_WF_RUN_SQL",
    "INSERT_WF_WINDOW_SQL",
    "FETCH_WF_RUN_SQL",
    "FETCH_WF_LATEST_SQL",
    "FETCH_WF_WINDOWS_SQL",
    "LIST_WF_RUNS_SQL",
    # Correlation Analysis
    "INSERT_CORRELATION_SQL",
    "FETCH_CORRELATION_BY_ID_SQL",
    "FETCH_CORRELATION_LATEST_SQL",
    "LIST_CORRELATION_BY_RUN_SQL",
]
