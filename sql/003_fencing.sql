-- 003: fencing tokens, heartbeats, and a registry of live workers.
ALTER TABLE jobs ADD COLUMN fencing_token bigint NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN heartbeat_at timestamptz;
ALTER TABLE job_attempts ADD COLUMN fencing_token bigint;

-- The reaper's question is "which held jobs have an expiry in the past"; only
-- held rows have an expiry, so a partial index stays tiny.
CREATE INDEX jobs_lease_expiry ON jobs (lease_expires_at) WHERE state IN ('LEASED', 'RUNNING');

CREATE TABLE workers (
    worker_id  text PRIMARY KEY,
    pid        int NOT NULL,
    host       text NOT NULL,
    port       int,
    started_at timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    status     text NOT NULL DEFAULT 'up',
    current_job bigint
);
