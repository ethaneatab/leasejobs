#!/usr/bin/env bash
# Stop what scripts/demo.sh started, by the PIDs it wrote. Never by name.
cd "$(dirname "$0")/.."
DC=${DC:-/opt/homebrew/bin/docker-compose}
command -v "$DC" >/dev/null 2>&1 || DC="docker compose"
for name in web supervisor scheduler api integration; do
  f=.runtime/$name.pid
  [ -f "$f" ] || continue
  pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
    echo "stopped $name ($pid)"
  fi
  rm -f "$f"
done
[ "${1:-}" = "--keep-containers" ] || $DC -p leasejobs stop
