-- 002: a lease is an owner plus an expiry time, both decided by the database clock.
ALTER TABLE jobs ADD COLUMN lease_expires_at timestamptz;
ALTER TABLE jobs ADD COLUMN leased_at timestamptz;

-- A side-effect sink: the `tally` job type writes one row per execution of its
-- body. Tests count rows here to prove "each job ran exactly once".
CREATE TABLE tally (
    id        bigserial PRIMARY KEY,
    job_id    bigint NOT NULL,
    worker_id text NOT NULL,
    at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
