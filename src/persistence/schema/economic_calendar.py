DDL = """
CREATE TABLE IF NOT EXISTS economic_calendar_events (
    scheduled_at TIMESTAMPTZ NOT NULL,
    event_uid TEXT NOT NULL,
    source TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    country TEXT,
    category TEXT,
    currency TEXT,
    reference TEXT,
    actual TEXT,
    previous TEXT,
    forecast TEXT,
    revised TEXT,
    importance INTEGER,
    unit TEXT,
    release_id TEXT,
    source_url TEXT,
    all_day BOOLEAN NOT NULL DEFAULT FALSE,
    scheduled_at_local TIMESTAMP,
    local_timezone TEXT,
    scheduled_at_release TIMESTAMP,
    release_timezone TEXT,
    session_bucket TEXT NOT NULL DEFAULT 'off_hours'
        CHECK (session_bucket IN ('asia', 'europe', 'us', 'off_hours')),
    is_asia_session BOOLEAN NOT NULL DEFAULT FALSE,
    is_europe_session BOOLEAN NOT NULL DEFAULT FALSE,
    is_us_session BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'released', 'cancelled', 'deleted')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at TIMESTAMPTZ,
    last_value_check_at TIMESTAMPTZ,
    raw_payload JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scheduled_at, event_uid)
);

SELECT create_hypertable('economic_calendar_events', 'scheduled_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_econ_events_time
    ON economic_calendar_events (scheduled_at DESC, source);

CREATE INDEX IF NOT EXISTS idx_econ_events_country
    ON economic_calendar_events (country, scheduled_at DESC);

CREATE INDEX IF NOT EXISTS idx_econ_events_session
    ON economic_calendar_events (session_bucket, scheduled_at DESC);

CREATE INDEX IF NOT EXISTS idx_econ_events_uid
    ON economic_calendar_events (event_uid, last_updated DESC);

CREATE INDEX IF NOT EXISTS idx_econ_events_status
    ON economic_calendar_events (status, scheduled_at DESC);

CREATE TABLE IF NOT EXISTS economic_calendar_event_updates (
    recorded_at TIMESTAMPTZ NOT NULL,
    event_uid TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    country TEXT,
    currency TEXT,
    status TEXT NOT NULL,
    snapshot_reason TEXT NOT NULL,
    job_type TEXT NOT NULL,
    actual TEXT,
    previous TEXT,
    forecast TEXT,
    revised TEXT,
    importance INTEGER,
    raw_payload JSONB,
    PRIMARY KEY (recorded_at, event_uid, snapshot_reason)
);

SELECT create_hypertable('economic_calendar_event_updates', 'recorded_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_econ_updates_uid
    ON economic_calendar_event_updates (event_uid, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_econ_updates_job
    ON economic_calendar_event_updates (job_type, recorded_at DESC);
"""

UPSERT_SQL = """
INSERT INTO economic_calendar_events (
    scheduled_at,
    event_uid,
    source,
    provider_event_id,
    event_name,
    country,
    category,
    currency,
    reference,
    actual,
    previous,
    forecast,
    revised,
    importance,
    unit,
    release_id,
    source_url,
    all_day,
    scheduled_at_local,
    local_timezone,
    scheduled_at_release,
    release_timezone,
    session_bucket,
    is_asia_session,
    is_europe_session,
    is_us_session,
    status,
    first_seen_at,
    last_seen_at,
    released_at,
    last_value_check_at,
    raw_payload,
    ingested_at,
    last_updated
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (scheduled_at, event_uid) DO UPDATE SET
    source = EXCLUDED.source,
    provider_event_id = EXCLUDED.provider_event_id,
    event_name = EXCLUDED.event_name,
    country = EXCLUDED.country,
    category = EXCLUDED.category,
    currency = EXCLUDED.currency,
    reference = EXCLUDED.reference,
    actual = EXCLUDED.actual,
    previous = EXCLUDED.previous,
    forecast = EXCLUDED.forecast,
    revised = EXCLUDED.revised,
    importance = EXCLUDED.importance,
    unit = EXCLUDED.unit,
    release_id = EXCLUDED.release_id,
    source_url = EXCLUDED.source_url,
    all_day = EXCLUDED.all_day,
    scheduled_at_local = EXCLUDED.scheduled_at_local,
    local_timezone = EXCLUDED.local_timezone,
    scheduled_at_release = EXCLUDED.scheduled_at_release,
    release_timezone = EXCLUDED.release_timezone,
    session_bucket = EXCLUDED.session_bucket,
    is_asia_session = EXCLUDED.is_asia_session,
    is_europe_session = EXCLUDED.is_europe_session,
    is_us_session = EXCLUDED.is_us_session,
    status = EXCLUDED.status,
    first_seen_at = COALESCE(economic_calendar_events.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at = EXCLUDED.last_seen_at,
    released_at = COALESCE(economic_calendar_events.released_at, EXCLUDED.released_at),
    last_value_check_at = EXCLUDED.last_value_check_at,
    raw_payload = EXCLUDED.raw_payload,
    ingested_at = COALESCE(economic_calendar_events.ingested_at, EXCLUDED.ingested_at),
    last_updated = EXCLUDED.last_updated
"""

INSERT_UPDATE_SQL = """
INSERT INTO economic_calendar_event_updates (
    recorded_at,
    event_uid,
    scheduled_at,
    source,
    provider_event_id,
    event_name,
    country,
    currency,
    status,
    snapshot_reason,
    job_type,
    actual,
    previous,
    forecast,
    revised,
    importance,
    raw_payload
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

DELETE_BY_KEYS_SQL = """
DELETE FROM economic_calendar_events
WHERE (scheduled_at, event_uid) IN %s
"""
