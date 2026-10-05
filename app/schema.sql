-- Vibration spike alert service schema.

CREATE TABLE IF NOT EXISTS devices (
    device_id    TEXT PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (device, sample). rev increases monotonically; a newer revision
-- may move the reading in time and/or change its amplitude. Old revisions are
-- ignored, same revision with different content is a 409 conflict.
CREATE TABLE IF NOT EXISTS readings (
    device_id    TEXT NOT NULL REFERENCES devices(device_id),
    sample_id    TEXT NOT NULL,
    rev          BIGINT NOT NULL,
    ts           BIGINT NOT NULL,                 -- event time, UTC unix whole seconds
    amplitude    NUMERIC(18,6) NOT NULL,          -- finite, >= 0
    baseline     NUMERIC(18,6),                   -- exact median of valid prior 60min window
    window_count INTEGER NOT NULL DEFAULT 0,      -- number of values feeding the baseline
    is_hit       BOOLEAN NOT NULL DEFAULT FALSE,  -- window_count >= 20 AND amplitude > 3*baseline
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, sample_id)
);

-- Pure-append hot path: device + event time.
CREATE INDEX IF NOT EXISTS readings_device_ts_idx
    ON readings (device_id, ts) INCLUDE (amplitude, sample_id);

-- Merged alerts. An alert spans a maximal run of consecutive hits (ordered by
-- event time, sample id tie-break) where adjacent hits are <= 300s apart.
-- alert_id is the sample id of the first hit in the run.
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT NOT NULL,                    -- first hit sample id
    device_id    TEXT NOT NULL REFERENCES devices(device_id),
    start_ts     BIGINT NOT NULL,
    end_ts       BIGINT NOT NULL,
    peak_ts      BIGINT NOT NULL,
    peak_amp     NUMERIC(18,6) NOT NULL,
    sample_ids   JSONB NOT NULL,                   -- ordered hit sample ids
    hit_count    INTEGER NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, alert_id)
);

-- Transactional outbox: one row per new / updated / revoked alert.
-- Written in the same transaction as readings + alerts, so a retried POST
-- (same revisions) creates no further changes.
CREATE TABLE IF NOT EXISTS alert_changes (
    seq          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id    TEXT NOT NULL,
    alert_id     TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('created','updated','revoked')),
    alert        JSONB,                          -- present for created/updated, null for revoked
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tolerate databases created when alert was NOT NULL.
ALTER TABLE alert_changes ALTER COLUMN alert DROP NOT NULL;

CREATE INDEX IF NOT EXISTS alert_changes_seq_idx ON alert_changes (seq);
