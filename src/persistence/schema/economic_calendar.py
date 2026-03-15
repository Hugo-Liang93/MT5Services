DDL = """
CREATE TABLE IF NOT EXISTS economic_calendar_events (
    scheduled_at timestamptz NOT NULL,
    event_uid text NOT NULL,
    source text NOT NULL,
    provider_event_id text NOT NULL,
    event_name text NOT NULL,
    country text,
    category text,
    currency text,
    reference text,
    actual text,
    previous text,
    forecast text,
    revised text,
    importance integer,
    unit text,
    release_id text,
    source_url text,
    all_day boolean NOT NULL DEFAULT false,
    scheduled_at_local timestamp,
    local_timezone text,
    scheduled_at_release timestamp,
    release_timezone text,
    session_bucket text NOT NULL DEFAULT 'off_hours',
    is_asia_session boolean NOT NULL DEFAULT false,
    is_europe_session boolean NOT NULL DEFAULT false,
    is_us_session boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'scheduled',
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    last_value_check_at timestamptz,
    raw_payload jsonb,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    last_updated timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheduled_at, event_uid)
);
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS scheduled_at_local timestamp;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS local_timezone text;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS scheduled_at_release timestamp;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS release_timezone text;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS session_bucket text NOT NULL DEFAULT 'off_hours';
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS is_asia_session boolean NOT NULL DEFAULT false;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS is_europe_session boolean NOT NULL DEFAULT false;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS is_us_session boolean NOT NULL DEFAULT false;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'scheduled';
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS first_seen_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS released_at timestamptz;
ALTER TABLE economic_calendar_events ADD COLUMN IF NOT EXISTS last_value_check_at timestamptz;
SELECT create_hypertable('economic_calendar_events', 'scheduled_at', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS economic_calendar_events_time_idx
ON economic_calendar_events (scheduled_at DESC, source);
CREATE INDEX IF NOT EXISTS economic_calendar_events_country_idx
ON economic_calendar_events (country, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS economic_calendar_events_session_idx
ON economic_calendar_events (session_bucket, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS economic_calendar_events_uid_idx
ON economic_calendar_events (event_uid, last_updated DESC);
CREATE INDEX IF NOT EXISTS economic_calendar_events_status_idx
ON economic_calendar_events (status, scheduled_at DESC);

CREATE TABLE IF NOT EXISTS economic_calendar_event_updates (
    recorded_at timestamptz NOT NULL,
    event_uid text NOT NULL,
    scheduled_at timestamptz NOT NULL,
    source text NOT NULL,
    provider_event_id text NOT NULL,
    event_name text NOT NULL,
    country text,
    currency text,
    status text NOT NULL,
    snapshot_reason text NOT NULL,
    job_type text NOT NULL,
    actual text,
    previous text,
    forecast text,
    revised text,
    importance integer,
    raw_payload jsonb,
    PRIMARY KEY (recorded_at, event_uid, snapshot_reason)
);
SELECT create_hypertable('economic_calendar_event_updates', 'recorded_at', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS economic_calendar_event_updates_uid_idx
ON economic_calendar_event_updates (event_uid, recorded_at DESC);
CREATE INDEX IF NOT EXISTS economic_calendar_event_updates_job_idx
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
