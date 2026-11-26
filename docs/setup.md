# Setup

Everything below was run on macOS with colima (4 CPU / 6 GiB shared with other
projects), Docker CLI + `/opt/homebrew/bin/docker-compose`, Python 3.11 and Node 24.

## One command

```bash
./scripts/demo.sh        # containers, venv, migrations, seed, all processes, web UI
./scripts/stop.sh        # stops exactly the PIDs demo.sh wrote, then `docker-compose -p leasejobs stop`
./scripts/stop.sh --keep-containers
```

`demo.sh` works on an empty volume (`docker-compose -p leasejobs down -v` first to prove it):
it runs `leasejobs.migrate` before `leasejobs.seed`, because the seed writes into tables the
migrations create.

## What runs where

| process | port | start by hand | pid file |
|---|---|---|---|
| Postgres 16 (container) | 54311 | `docker-compose -p leasejobs up -d --wait postgres` | – |
| Prometheus 3.5 (container) | 9211 | `docker-compose -p leasejobs up -d prometheus` | – |
| API | 8910 | `.venv/bin/python -m leasejobs.api` | `.runtime/api.pid` |
| workers (via supervisor) | 8911–8913 | `.venv/bin/python -m leasejobs.supervisor --workers 3` | `.runtime/supervisor.pid`, `.runtime/worker-N.pid` |
| scheduler | 8916 | `.venv/bin/python -m leasejobs.scheduler --id scheduler-1 --port 8916` | `.runtime/scheduler.pid` |
| fake integration | 8915 | `.venv/bin/python -m leasejobs.integration --port 8915` | `.runtime/integration.pid` |
| web UI (Vite) | 5191 | `npm --prefix frontend run dev -- --port 5191` | `.runtime/web.pid` |

Tests use 8917 (nothing listens: "dependency down"), 8918 (their own fake integration) and
8919 (supervisor test), so they can run while the demo is up.

Logs are JSON lines in `var/*.log`, one object per line with `ts level component pid msg` first.

## Configuration (environment)

| variable | default | meaning |
|---|---|---|
| `LEASEJOBS_DSN` | `postgresql://leasejobs:leasejobs-dev@127.0.0.1:54311/leasejobs` | database |
| `LEASEJOBS_DEV_KEYS` | `alice=dev-key-alice,bob=dev-key-bob` | owner=key pairs seeded (dev placeholders) |
| `LEASEJOBS_LEASE_TTL_S` | 4 | lease length; a silent worker loses its job this long after its last heartbeat |
| `LEASEJOBS_HEARTBEAT_S` | 1 | heartbeat period (keep it well under the TTL) |
| `LEASEJOBS_POLL_S` | 0.25 | idle worker poll interval |
| `LEASEJOBS_RETRY_BASE_S` / `_CAP_S` | 1 / 60 | retry backoff |
| `LEASEJOBS_CACHE_SIZE` | 256 | result cache entries per API process |
| `LEASEJOBS_INTEGRATION_URL` | `http://127.0.0.1:8915` | where `call_integration` writes |
| `LEASEJOBS_LAB_SABOTAGE` | empty | switch protections off on purpose: `no_reclaim,no_fencing,no_skip_locked,no_ratelimit` |

## Tests

```bash
docker-compose -p leasejobs up -d --wait postgres
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                 # recreates database leasejobs_test each session
./scripts/failing_controls.sh             # each protection off -> red, on -> green
npm ci && npx playwright test -c e2e      # needs ./scripts/demo.sh running
```

## Grafana

Not started (memory budget). `deploy/grafana/leasejobs-dashboard.json` imports into any Grafana
with a Prometheus datasource pointed at http://localhost:9211.
