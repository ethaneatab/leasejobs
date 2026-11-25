-- 001: jobs, attempts, events, owners, and the state machine enforced in SQL.

CREATE TABLE api_keys (
    key_hash   text PRIMARY KEY,           -- sha256 hex of the key; the key itself is never stored
    owner      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id               bigserial PRIMARY KEY,
    owner            text NOT NULL,
    type             text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    state            text NOT NULL,
    priority         int NOT NULL DEFAULT 0,
    attempts         int NOT NULL DEFAULT 0,
    max_attempts     int NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
    timeout_s        double precision NOT NULL DEFAULT 30 CHECK (timeout_s > 0),
    run_at           timestamptz NOT NULL DEFAULT now(),
    lease_owner      text,
    result           jsonb,
    last_error       text,
    cancel_requested boolean NOT NULL DEFAULT false,
    idempotency_key  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    UNIQUE (owner, idempotency_key)
);

-- Workers look for runnable work by state and time; keep that lookup an index scan.
CREATE INDEX jobs_runnable ON jobs (state, run_at, priority DESC, id);
CREATE INDEX jobs_owner ON jobs (owner, id DESC);

CREATE TABLE job_attempts (
    id          bigserial PRIMARY KEY,
    job_id      bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_no  int NOT NULL,
    worker_id   text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    outcome     text NOT NULL DEFAULT 'running',
    error       text,
    UNIQUE (job_id, attempt_no)
);

-- Every state change and every notable lease event, in order. The learn page
-- and the lease timeline are both built from this table.
CREATE TABLE job_events (
    id         bigserial PRIMARY KEY,
    job_id     bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    kind       text NOT NULL,
    from_state text,
    to_state   text,
    actor      text NOT NULL,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX job_events_job ON job_events (job_id, id);

-- @learn:sql-transitions
-- The allowed edges of the job state machine. leasejobs/states.py holds the
-- same table; tests/test_states.py fails if the two ever disagree.
CREATE TABLE job_transitions (
    from_state text NOT NULL,
    to_state   text NOT NULL,
    PRIMARY KEY (from_state, to_state)
);
INSERT INTO job_transitions (from_state, to_state) VALUES
    ('SCHEDULED', 'PENDING'), ('SCHEDULED', 'CANCELLED'),
    ('PENDING', 'LEASED'), ('PENDING', 'CANCELLED'),
    ('LEASED', 'RUNNING'), ('LEASED', 'PENDING'), ('LEASED', 'SCHEDULED'), ('LEASED', 'CANCELLED'),
    ('RUNNING', 'SUCCEEDED'), ('RUNNING', 'FAILED_RETRYABLE'), ('RUNNING', 'FAILED_TERMINAL'),
    ('RUNNING', 'TIMED_OUT'), ('RUNNING', 'CANCELLED'), ('RUNNING', 'PENDING'),
    ('FAILED_RETRYABLE', 'SCHEDULED');

CREATE FUNCTION jobs_guard_transition() RETURNS trigger AS $$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT EXISTS (
        SELECT 1 FROM job_transitions WHERE from_state = OLD.state AND to_state = NEW.state
    ) THEN
        RAISE EXCEPTION 'illegal job transition % -> % (job %)', OLD.state, NEW.state, OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER jobs_guard_transition BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION jobs_guard_transition();
-- @learn:end sql-transitions

ALTER TABLE jobs ADD CONSTRAINT jobs_state_known CHECK (state IN (
    'SCHEDULED', 'PENDING', 'LEASED', 'RUNNING', 'SUCCEEDED',
    'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'TIMED_OUT', 'CANCELLED'));
