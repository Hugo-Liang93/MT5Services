"""auto_executions 表 DDL — 记录 TradeExecutor 的每次自动交易尝试。

每条记录对应一次 on_signal_event 触发的自动交易：
- 成功记录：包含下单参数、止损止盈、风险收益比。
- 失败记录：包含错误信息，用于排查熔断触发原因。
"""

DDL = """
CREATE TABLE IF NOT EXISTS auto_executions (
    executed_at     timestamptz NOT NULL,
    signal_id       text NOT NULL,
    symbol          text NOT NULL,
    action          text NOT NULL,
    strategy        text NOT NULL,
    confidence      double precision,
    volume          double precision,
    entry_price     double precision,
    stop_loss       double precision,
    take_profit     double precision,
    risk_reward     double precision,
    success         boolean NOT NULL,
    error_message   text,
    metadata        jsonb,
    PRIMARY KEY (signal_id, executed_at)
);
CREATE INDEX IF NOT EXISTS auto_executions_symbol_idx
ON auto_executions (symbol, executed_at DESC);
CREATE INDEX IF NOT EXISTS auto_executions_success_idx
ON auto_executions (success, executed_at DESC);
"""

INSERT_SQL = """
INSERT INTO auto_executions (
    executed_at,
    signal_id,
    symbol,
    action,
    strategy,
    confidence,
    volume,
    entry_price,
    stop_loss,
    take_profit,
    risk_reward,
    success,
    error_message,
    metadata
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (signal_id, executed_at) DO NOTHING
"""
