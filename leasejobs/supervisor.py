"""Start N worker processes and restart any that die.

`python -m leasejobs.supervisor --workers 3 --base-port 8911`

A restarted worker gets a new id (worker-2.g2, worker-2.g3, ...), so the lease
timeline shows which incarnation held a job. The jobs the dead one held are
not handed over by the supervisor: their leases expire and whichever worker
reaps first takes them. That is the point: recovery must not depend on the
process that died, or on this one.
Child PIDs are written to .runtime/worker-N.pid; on SIGTERM we stop exactly
those PIDs.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .logs import JsonLog

ROOT = Path(__file__).resolve().parent.parent
log = JsonLog("supervisor")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--base-port", type=int, default=8911)
    ap.add_argument("--restart-delay", type=float, default=2.0)
    ap.add_argument("--runtime-dir", type=Path, default=ROOT / ".runtime", help="where pid files go")
    args = ap.parse_args()
    runtime = args.runtime_dir
    if not 1 <= args.workers <= 4:
        sys.exit("1-4 workers (health ports 8911-8914)")
    runtime.mkdir(exist_ok=True, parents=True)
    logs = runtime.parent / "var" if runtime != ROOT / ".runtime" else ROOT / "var"
    logs.mkdir(exist_ok=True)
    gen = {n: 0 for n in range(1, args.workers + 1)}
    procs: dict[int, subprocess.Popen] = {}
    died_at: dict[int, float] = {}
    stopping = False

    def spawn(n: int) -> None:
        gen[n] += 1
        wid = f"worker-{n}" if gen[n] == 1 else f"worker-{n}.g{gen[n]}"
        out = open(logs / f"worker-{n}.log", "ab")
        p = subprocess.Popen([sys.executable, "-m", "leasejobs.worker", "--id", wid,
                              "--port", str(args.base_port + n - 1)], cwd=ROOT, stdout=out, stderr=out)
        procs[n] = p
        (runtime / f"worker-{n}.pid").write_text(str(p.pid))
        log.info("worker started", worker=wid, pid=p.pid, port=args.base_port + n - 1)

    def on_term(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    for n in gen:
        spawn(n)
    while not stopping:
        for n, p in list(procs.items()):
            rc = p.poll()
            if rc is None:
                continue
            if n not in died_at:
                died_at[n] = time.monotonic()
                log.warn("worker exited", n=n, pid=p.pid, returncode=rc)
            elif time.monotonic() - died_at[n] >= args.restart_delay:
                del died_at[n]
                spawn(n)
        (runtime / "supervised.json").write_text(json.dumps({n: p.pid for n, p in procs.items()}))
        time.sleep(0.2)
    for p in procs.values():
        if p.poll() is None:
            os.kill(p.pid, signal.SIGCONT)  # a frozen worker can't act on SIGTERM
            os.kill(p.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    for p in procs.values():
        try:
            p.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.kill(p.pid, signal.SIGKILL)
    for n in gen:
        (runtime / f"worker-{n}.pid").unlink(missing_ok=True)
    log.info("supervisor stopped")


if __name__ == "__main__":
    main()
