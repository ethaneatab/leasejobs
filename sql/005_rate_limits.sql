-- 005: one shared token bucket per external integration, in Postgres.
CREATE TABLE integrations (
    name         text PRIMARY KEY,
    capacity     double precision NOT NULL CHECK (capacity >= 1),
    refill_per_s double precision NOT NULL CHECK (refill_per_s > 0),
    tokens       double precision NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
INSERT INTO integrations (name, capacity, refill_per_s, tokens) VALUES
    ('ledger-api', 5, 2, 5),
    ('slow-crm', 2, 0.5, 2);

ALTER TABLE jobs ADD COLUMN deferrals int NOT NULL DEFAULT 0;
