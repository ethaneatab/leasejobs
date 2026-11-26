-- 007: teaching mode. A learn run is one sample job plus the moment it started;
-- everything shown on /learn is read back from jobs, job_events and job_attempts.
CREATE TABLE learn_runs (
    id         bigserial PRIMARY KEY,
    job_id     bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    owner      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
