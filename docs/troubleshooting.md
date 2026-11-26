# Troubleshooting

Each section: what you see, the command that tells you which case you are in, what to do.
All commands are run from the repo root against the demo (`./scripts/demo.sh`).

```bash
H='X-API-Key: dev-key-alice'
```

## Jobs stuck in RUNNING or LEASED

Look at why the system thinks they are stuck:

```bash
curl -s localhost:8910/stalled -H "$H"
```

- `lease_expired_unreclaimed`: the lease ran out more than 2×TTL ago and nobody took it back.
  Reaping is done by live workers before each claim, so this means no worker is looping.
  `curl -s localhost:8910/workers -H "$H"` – are any `alive`? If not, check the supervisor:
  `cat .runtime/supervisor.pid; tail -5 var/supervisor.log`. Also check you did not leave
  `LEASEJOBS_LAB_SABOTAGE=no_reclaim` in the environment (`tail -1 var/worker-1.log` shows the
  worker's start line).
- `running_over_timeout`: a worker is heartbeating but the attempt is past `timeout_s`. The worker
  enforces the deadline itself (`run_with_deadline`), so a live worker in this state is a bug: grab
  `tail -50 var/worker-N.log` and the job's events.

The full lease history of one job:

```bash
curl -s localhost:8910/jobs/42/events -H "$H" | .venv/bin/python -m json.tool | grep -E '"kind"|"actor"|fencing'
```

## Many `stale_write_rejected` events / `LeasejobsStaleWritesSpike`

Fencing is working, but leases are expiring under workers that are still alive. Usual causes:
the TTL is too close to the heartbeat period, the worker host is overloaded (heartbeat thread
starved), or Postgres is slow to answer the heartbeat UPDATE. Compare `LEASEJOBS_LEASE_TTL_S`
with `LEASEJOBS_HEARTBEAT_S` (default 4 vs 1), then:

```bash
curl -s localhost:8911/metrics | grep -E 'lease_lost|stale_writes'
docker stats --no-stream leasejobs-postgres-1
```

## Delayed jobs never start (`scheduled_overdue`)

No scheduler holds the leader lock, or it is not getting notifications.

```bash
curl -s localhost:8916/readyz                    # leader, heap_size, tick_age_s
curl -s localhost:8910/scheduler -H "$H"         # last snapshot written to Postgres and its age_s
docker exec leasejobs-postgres-1 psql -U leasejobs -c \
  "SELECT pid, granted FROM pg_locks WHERE locktype = 'advisory' AND objid = 7310002"
```

If a scheduler is running but `leader` is false, another session holds advisory lock 7310002;
the standby takes over within ~2 s of that session ending. A lost NOTIFY heals itself at the next
resync (every 15 s).

## Jobs keep bouncing between LEASED and SCHEDULED

That is rate limiting doing its job: `kind = rate_limited` events with `wait_s`. Check the bucket:

```bash
curl -s localhost:8910/integrations -H "$H"
curl -s localhost:8911/metrics | grep rate_limited_total
```

If the backlog for one integration keeps growing, the configured rate is below the arrival rate;
raise `refill_per_s` in the `integrations` table only if the provider really allows it.

## Calls to the integration fail

```bash
curl -s localhost:8915/healthz
curl -s localhost:8915/ledger-api/ledger/job-42      # accepted / rejected writes for one key
curl -s -XPOST localhost:8915/admin/mode -d '{"mode":"ok"}'   # clear injected faults
```

5xx and "unreachable" retry with backoff until `max_attempts`; 4xx fails the job at once; 409 means
the integration has seen a newer fencing token for that key, i.e. another worker owns the job now.

## `GET /jobs/<id>/result` is slow on the first call

`X-Cache: MISS` on the first read after an API restart is expected (the cache is per process, in
memory). If every read misses, the warm-up listener is not running: look for
`cache listener reconnecting` in `var/api.log`.

## Tests fail with connection refused on 54311

`docker-compose -p leasejobs up -d --wait postgres`. The test session drops and recreates
`leasejobs_test` (`WITH (FORCE)`), so a psql session left open on it will be disconnected.
