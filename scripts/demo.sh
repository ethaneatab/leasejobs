#!/usr/bin/env bash
# From nothing to a running leasejobs, in one command. Works on an empty volume:
# containers -> migrations -> seed -> integration, API, scheduler, 3 workers -> web UI.
# Every process we start writes .runtime/<name>.pid; scripts/stop.sh stops exactly those.
set -euo pipefail
cd "$(dirname "$0")/.."
DC=${DC:-/opt/homebrew/bin/docker-compose}
command -v "$DC" >/dev/null 2>&1 || DC="docker compose"
PY=.venv/bin/python
mkdir -p .runtime var

echo "== containers (compose project leasejobs)"
$DC -p leasejobs up -d --wait

if [ ! -x "$PY" ]; then
  echo "== creating .venv"
  PYBIN=${PYBIN:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11}
  command -v "$PYBIN" >/dev/null 2>&1 || PYBIN=python3.11
  "$PYBIN" -m venv .venv
fi
$PY -m pip install -q -r requirements.txt

echo "== schema and dev keys"
$PY -m leasejobs.migrate          # tables, triggers, buckets
$PY -m leasejobs.seed             # api keys (needs the tables, so after migrate)

start() { # name logfile command...
  local name=$1 logf=$2; shift 2
  local pidf=.runtime/$name.pid
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then echo "   $name already running (pid $(cat "$pidf"))"; return; fi
  nohup "$@" >>"var/$logf" 2>&1 &
  echo $! >"$pidf"
  echo "   $name pid $!"
}
wait_http() { # url what
  for _ in $(seq 1 60); do curl -sf -o /dev/null "$1" && return 0; sleep 0.25; done
  echo "!! $2 did not come up ($1); see var/*.log" >&2; exit 1
}

echo "== processes"
start integration integration.log $PY -m leasejobs.integration --port 8915
start api api.log $PY -m leasejobs.api
start scheduler scheduler.log $PY -m leasejobs.scheduler --id scheduler-1 --port 8916
start supervisor supervisor.log $PY -m leasejobs.supervisor --workers 3 --base-port 8911
wait_http http://127.0.0.1:8915/healthz integration
wait_http http://127.0.0.1:8910/readyz api
wait_http http://127.0.0.1:8916/readyz scheduler
for p in 8911 8912 8913; do wait_http http://127.0.0.1:$p/readyz "worker :$p"; done

if [ -d frontend ] && [ -f frontend/package.json ]; then
  echo "== web UI"
  [ -d frontend/node_modules ] || (cd frontend && npm ci --silent)
  [ -d node_modules ] || npm ci --silent
  node scripts/learn_anchors.mjs >/dev/null
  # exec: the PID we record is vite itself, not an npm wrapper whose child would outlive it
  start web web.log sh -c 'cd frontend && exec node node_modules/vite/bin/vite.js --port 5191 --strictPort'
  wait_http http://127.0.0.1:5191/ "web UI"
fi

cat <<MSG

  web UI       http://localhost:5191        (/learn, /learn/interview, /learn/problems)
  API          http://localhost:8910        X-API-Key: dev-key-alice
  workers      :8911 :8912 :8913            /healthz /readyz /metrics
  scheduler    :8916     integration :8915
  Prometheus   http://localhost:9211        Postgres localhost:54311

  stop:        ./scripts/stop.sh
MSG
