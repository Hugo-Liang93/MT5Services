"""signal_outcomes 表 DDL — 记录信号事后结果，用于胜率统计。

每条记录对应一个已发出的 confirmed 信号，在信号后 N 根 bar 收盘后
由 OutcomeTracker 回填价格变动，标记信号是否"获胜"。
"""

DDL = """
CREATE TABLE IF NOT EXISTS signal_outcomes (
    recorded_at   timestamptz NOT NULL,
    signal_id     text NOT NULL,
    symbol        text NOT NULL,
    timeframe     text NOT NULL,
    strategy      text NOT NULL,
    action        text NOT NULL,
    confidence    double precision NOT NULL,
    entry_price   double precision,
    exit_price    double precision,
    price_change  double precision,
    won           boolean,
    bars_held     int,
    regime        text,
    metadata      jsonb,
    PRIMARY KEY (signal_id)
);
CREATE INDEX IF NOT EXISTS signal_outcomes_symbol_idx
ON signal_outcomes (symbol, timeframe, strategy, recorded_at DESC);
CREATE INDEX IF NOT EXISTS signal_outcomes_won_idx
ON signal_outcomes (won, strategy, recorded_at DESC);
"""

INSERT_SQL = """
INSERT INTO signal_outcomes (
    recorded_at,
    signal_id,
    symbol,
    timeframe,
    strategy,
    action,
    confidence,
    entry_price,
    exit_price,
    price_change,
    won,
    bars_held,
    regime,
    metadata
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (signal_id) DO UPDATE SET
    exit_price    = EXCLUDED.exit_price,
    price_change  = EXCLUDED.price_change,
    won           = EXCLUDED.won,
    bars_held     = EXCLUDED.bars_held,
    metadata      = EXCLUDED.metadata
"""

WINRATE_SQL = """
SELECT
    strategy,
    action,
    COUNT(*)                                    AS total,
    SUM(CASE WHEN won THEN 1 ELSE 0 END)        AS wins,
    ROUND(
        AVG(CASE WHEN won THEN 1.0 ELSE 0.0 END)::numeric, 4
    )                                           AS win_rate,
    ROUND(AVG(confidence)::numeric, 4)          AS avg_confidence,
    ROUND(AVG(ABS(price_change))::numeric, 6)   AS avg_move
FROM signal_outcomes
WHERE recorded_at >= NOW() - make_interval(hours => %s)
  AND won IS NOT NULL
  AND (%s IS NULL OR symbol = %s)
GROUP BY strategy, action
ORDER BY win_rate DESC, total DESC
"""

EXPECTANCY_SQL = """
SELECT
    strategy,
    action,
    COUNT(*)                                              AS total,
    SUM(CASE WHEN won THEN 1 ELSE 0 END)                  AS wins,
    SUM(CASE WHEN won = FALSE THEN 1 ELSE 0 END)          AS losses,
    ROUND(
        AVG(CASE WHEN won THEN 1.0 ELSE 0.0 END)::numeric, 4
    )                                                     AS win_rate,
    ROUND(
        AVG(CASE WHEN won THEN ABS(price_change) END)::numeric, 6
    )                                                     AS avg_win_move,
    ROUND(
        AVG(CASE WHEN won = FALSE THEN ABS(price_change) END)::numeric, 6
    )                                                     AS avg_loss_move,
    ROUND(
        (
            (
                COALESCE(AVG(CASE WHEN won THEN ABS(price_change) END), 0.0)
                * AVG(CASE WHEN won THEN 1.0 ELSE 0.0 END)
            ) - (
                COALESCE(AVG(CASE WHEN won = FALSE THEN ABS(price_change) END), 0.0)
                * (1.0 - AVG(CASE WHEN won THEN 1.0 ELSE 0.0 END))
            )
        )::numeric,
        6
    )                                                     AS expectancy,
    ROUND(
        (
            COALESCE(AVG(CASE WHEN won THEN ABS(price_change) END), 0.0)
            / NULLIF(AVG(CASE WHEN won = FALSE THEN ABS(price_change) END), 0.0)
        )::numeric,
        4
    )                                                     AS payoff_ratio
FROM signal_outcomes
WHERE recorded_at >= NOW() - make_interval(hours => %s)
  AND won IS NOT NULL
  AND (%s IS NULL OR symbol = %s)
GROUP BY strategy, action
ORDER BY expectancy DESC, total DESC
"""
