#!/usr/bin/env bash
# Prove the hard tests are alive: switch each protection off and watch the test
# that guards it go red, then switch it back on and watch it go green.
# Writes docs/results/failing-controls.txt. Needs Postgres on :54311.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
OUT=docs/results/failing-controls.txt
: > "$OUT"
status=0

run() { # name sabotage expect(red|green) pytest-args...
  local name=$1 sab=$2 expect=$3; shift 3
  local log; log=$(mktemp)
  LEASEJOBS_LAB_SABOTAGE=$sab $PY -m pytest -p no:cacheprovider "$@" >"$log" 2>&1
  local rc=$?
  local got=green; [ $rc -ne 0 ] && got=red
  local verdict=OK; [ "$got" != "$expect" ] && { verdict=UNEXPECTED; status=1; }
  {
    echo "=== $name  LEASEJOBS_LAB_SABOTAGE=${sab:-<none>}  expect=$expect got=$got  [$verdict]"
    echo "\$ pytest $*"
    grep -E "^E  +(AssertionError|assert )|^FAILED|passed|failed" "$log" | grep -v "^E  +Use -v" | head -8
    echo
  } | tee -a "$OUT"
  rm -f "$log"
}

KILL=tests/test_recovery.py::test_kill9_worker_job_is_reclaimed_with_history_intact
STALE=tests/test_recovery.py::test_stale_holder_cannot_heartbeat_or_finish
STOP=tests/test_recovery.py::test_sigstop_worker_revives_and_is_fenced_out

run "lease reclaim (kill -9 recovery)" no_reclaim red   "$KILL"
run "lease reclaim (kill -9 recovery)" ""         green "$KILL"
run "fencing token (in-process stale write)" no_fencing red   "$STALE"
run "fencing token (in-process stale write)" ""         green "$STALE"
run "fencing token (SIGSTOP + revive, downstream)" no_fencing red   "$STOP"
run "fencing token (SIGSTOP + revive, downstream)" ""         green "$STOP"
exit $status
