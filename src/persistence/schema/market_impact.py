DDL = """
CREATE TABLE IF NOT EXISTS economic_event_market_impact (
    recorded_at         timestamptz NOT NULL,
    event_uid           text NOT NULL,
    symbol              text NOT NULL,
    timeframe           text NOT NULL,

    event_name          text NOT NULL,
    country             text,
    currency            text,
    importance          integer,
    scheduled_at        timestamptz NOT NULL,
    released_at         timestamptz,

    actual              text,
    forecast            text,
    previous            text,
    surprise_pct        double precision,

    pre_price           double precision,
    pre_30m_change      double precision,
    pre_30m_range       double precision,
    pre_60m_change      double precision,
    pre_60m_range       double precision,
    pre_120m_change     double precision,
    pre_120m_range      double precision,

    post_5m_change      double precision,
    post_5m_range       double precision,
    post_15m_change     double precision,
    post_15m_range      double precision,
    post_30m_change     double precision,
    post_30m_range      double precision,
    post_60m_change     double precision,
    post_60m_range      double precision,
    post_120m_change    double precision,
    post_120m_range     double precision,

    volatility_pre_atr  double precision,
    volatility_post_atr double precision,
    volatility_spike    double precision,

    analysis_status     text NOT NULL DEFAULT 'pending',
    metadata            jsonb,

    PRIMARY KEY (event_uid, symbol, timeframe)
);

SELECT create_hypertable('economic_event_market_impact', 'recorded_at',
                          if_not_exists => TRUE, migrate_data => TRUE);

CREATE INDEX IF NOT EXISTS eemi_event_idx
ON economic_event_market_impact (event_name, symbol, recorded_at DESC);
CREATE INDEX IF NOT EXISTS eemi_country_idx
ON economic_event_market_impact (country, importance DESC, recorded_at DESC);
CREATE INDEX IF NOT EXISTS eemi_status_idx
ON economic_event_market_impact (analysis_status, recorded_at DESC);
"""

UPSERT_SQL = """
INSERT INTO economic_event_market_impact (
    recorded_at, event_uid, symbol, timeframe,
    event_name, country, currency, importance, scheduled_at, released_at,
    actual, forecast, previous, surprise_pct,
    pre_price,
    pre_30m_change, pre_30m_range,
    pre_60m_change, pre_60m_range,
    pre_120m_change, pre_120m_range,
    post_5m_change, post_5m_range,
    post_15m_change, post_15m_range,
    post_30m_change, post_30m_range,
    post_60m_change, post_60m_range,
    post_120m_change, post_120m_range,
    volatility_pre_atr, volatility_post_atr, volatility_spike,
    analysis_status, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s
) ON CONFLICT (event_uid, symbol, timeframe) DO UPDATE SET
    recorded_at = EXCLUDED.recorded_at,
    released_at = COALESCE(EXCLUDED.released_at, economic_event_market_impact.released_at),
    actual = COALESCE(EXCLUDED.actual, economic_event_market_impact.actual),
    forecast = COALESCE(EXCLUDED.forecast, economic_event_market_impact.forecast),
    previous = COALESCE(EXCLUDED.previous, economic_event_market_impact.previous),
    surprise_pct = COALESCE(EXCLUDED.surprise_pct, economic_event_market_impact.surprise_pct),
    pre_price = COALESCE(EXCLUDED.pre_price, economic_event_market_impact.pre_price),
    pre_30m_change = COALESCE(EXCLUDED.pre_30m_change, economic_event_market_impact.pre_30m_change),
    pre_30m_range = COALESCE(EXCLUDED.pre_30m_range, economic_event_market_impact.pre_30m_range),
    pre_60m_change = COALESCE(EXCLUDED.pre_60m_change, economic_event_market_impact.pre_60m_change),
    pre_60m_range = COALESCE(EXCLUDED.pre_60m_range, economic_event_market_impact.pre_60m_range),
    pre_120m_change = COALESCE(EXCLUDED.pre_120m_change, economic_event_market_impact.pre_120m_change),
    pre_120m_range = COALESCE(EXCLUDED.pre_120m_range, economic_event_market_impact.pre_120m_range),
    post_5m_change = COALESCE(EXCLUDED.post_5m_change, economic_event_market_impact.post_5m_change),
    post_5m_range = COALESCE(EXCLUDED.post_5m_range, economic_event_market_impact.post_5m_range),
    post_15m_change = COALESCE(EXCLUDED.post_15m_change, economic_event_market_impact.post_15m_change),
    post_15m_range = COALESCE(EXCLUDED.post_15m_range, economic_event_market_impact.post_15m_range),
    post_30m_change = COALESCE(EXCLUDED.post_30m_change, economic_event_market_impact.post_30m_change),
    post_30m_range = COALESCE(EXCLUDED.post_30m_range, economic_event_market_impact.post_30m_range),
    post_60m_change = COALESCE(EXCLUDED.post_60m_change, economic_event_market_impact.post_60m_change),
    post_60m_range = COALESCE(EXCLUDED.post_60m_range, economic_event_market_impact.post_60m_range),
    post_120m_change = COALESCE(EXCLUDED.post_120m_change, economic_event_market_impact.post_120m_change),
    post_120m_range = COALESCE(EXCLUDED.post_120m_range, economic_event_market_impact.post_120m_range),
    volatility_pre_atr = COALESCE(EXCLUDED.volatility_pre_atr, economic_event_market_impact.volatility_pre_atr),
    volatility_post_atr = COALESCE(EXCLUDED.volatility_post_atr, economic_event_market_impact.volatility_post_atr),
    volatility_spike = COALESCE(EXCLUDED.volatility_spike, economic_event_market_impact.volatility_spike),
    analysis_status = EXCLUDED.analysis_status,
    metadata = COALESCE(EXCLUDED.metadata, economic_event_market_impact.metadata)
"""

FETCH_BY_EVENT_SQL = """
SELECT * FROM economic_event_market_impact
WHERE event_uid = %s AND symbol = %s
ORDER BY recorded_at DESC
LIMIT 1
"""

AGGREGATED_STATS_SQL = """
SELECT
    event_name,
    country,
    importance,
    symbol,
    timeframe,
    COUNT(*) as sample_count,
    AVG(post_5m_change) as avg_post_5m_change,
    AVG(post_15m_change) as avg_post_15m_change,
    AVG(post_30m_change) as avg_post_30m_change,
    AVG(post_60m_change) as avg_post_60m_change,
    AVG(ABS(post_5m_range)) as avg_post_5m_range,
    AVG(ABS(post_15m_range)) as avg_post_15m_range,
    AVG(ABS(post_30m_range)) as avg_post_30m_range,
    AVG(ABS(post_60m_range)) as avg_post_60m_range,
    COUNT(CASE WHEN post_30m_change > 0 THEN 1 END)::float / NULLIF(COUNT(*), 0) as bullish_pct,
    AVG(volatility_spike) as avg_volatility_spike,
    MAX(volatility_spike) as max_volatility_spike,
    AVG(CASE WHEN surprise_pct > 0 THEN post_30m_change END) as surprise_positive_avg_change,
    AVG(CASE WHEN surprise_pct < 0 THEN post_30m_change END) as surprise_negative_avg_change
FROM economic_event_market_impact
WHERE analysis_status = 'complete'
    AND symbol = %s
    AND timeframe = %s
    AND (%s IS NULL OR event_name = %s)
    AND (%s IS NULL OR country = %s)
    AND (%s IS NULL OR importance >= %s)
GROUP BY event_name, country, importance, symbol, timeframe
ORDER BY sample_count DESC
LIMIT %s
"""
