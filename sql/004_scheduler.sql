-- 004: delayed jobs and recurring schedules. The rows here are the truth;
-- the scheduler's in-memory heap is only an index over them.
ALTER TABLE jobs ADD COLUMN schedule_id bigint;
ALTER TABLE jobs ADD COLUMN scheduled_for timestamptz;
-- One job per schedule slot, no matter how many times a scheduler retries the fire.
CREATE UNIQUE INDEX jobs_schedule_slot ON jobs (schedule_id, scheduled_for) WHERE schedule_id IS NOT NULL;

CREATE TABLE schedules (
    id            bigserial PRIMARY KEY,
    owner         text NOT NULL,
    name          text NOT NULL,
    cron          text,
    every_s       double precision CHECK (every_s IS NULL OR every_s >= 1),
    job_type      text NOT NULL,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    max_attempts  int NOT NULL DEFAULT 3,
    timeout_s     double precision NOT NULL DEFAULT 30,
    misfire       text NOT NULL DEFAULT 'skip' CHECK (misfire IN ('skip', 'catch_up')),
    max_catchup   int NOT NULL DEFAULT 10 CHECK (max_catchup BETWEEN 1 AND 1000),
    next_run_at   timestamptz NOT NULL,
    last_fired_at timestamptz,
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK ((cron IS NULL) <> (every_s IS NULL)),
    UNIQUE (owner, name)
);

CREATE TABLE scheduler_state (
    id         int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    leader     text,
    heap_size  int NOT NULL DEFAULT 0,
    top        jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- @learn:sql-notify
-- Whoever puts a job into SCHEDULED (API, retry, rate limiter) wakes the
-- scheduler for free: NOTIFY is delivered when that transaction commits.
CREATE FUNCTION notify_scheduler() RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'jobs' THEN
        PERFORM pg_notify('leasejobs_scheduler', 'job:' || NEW.id || ':' || extract(epoch FROM NEW.run_at));
    ELSE
        PERFORM pg_notify('leasejobs_scheduler', 'schedule:' || NEW.id || ':' ||
                          CASE WHEN NEW.enabled THEN extract(epoch FROM NEW.next_run_at)::text ELSE 'off' END);
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER jobs_notify_scheduler AFTER INSERT OR UPDATE OF state, run_at ON jobs
    FOR EACH ROW WHEN (NEW.state = 'SCHEDULED') EXECUTE FUNCTION notify_scheduler();
CREATE TRIGGER schedules_notify_scheduler AFTER INSERT OR UPDATE OF next_run_at, enabled ON schedules
    FOR EACH ROW EXECUTE FUNCTION notify_scheduler();
-- @learn:end sql-notify
