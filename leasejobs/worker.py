"""A worker process.

Loop: reap expired leases -> claim one job -> start it -> run it while a
heartbeat thread keeps the lease alive -> record the outcome with a fenced
write. One job at a time per process; run more processes for more throughput.

`python -m leasejobs.worker --id worker-1 --port 8911`
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import jobs as jobtypes
from . import store
from .config import settings
from .db import connect
from .lease import LeaseLost, LeaseManager
from .logs import JsonLog
from .states import State


class AttemptTimeout(Exception):
    pass


class Cancelled(Exception):
    pass


class JobContext:
    """What a running job can see and ask: its attempt, its token, and check()."""

    def __init__(self, job: dict, worker: "Worker", deadline: float):
        self.job = job
        self.attempt_no = job["attempts"]
        self.fencing_token = job["fencing_token"]
        self.worker_id = worker.worker_id
        self.conn = worker.conn
        self.integration_url = worker.cfg.integration_url
        self.deadline = deadline
        self.lease_lost = threading.Event()
        self.cancelled = threading.Event()

    def check(self) -> None:
        if self.lease_lost.is_set():
            raise LeaseLost(f"job {self.job['id']} lease lost")
        if self.cancelled.is_set():
            raise Cancelled(f"job {self.job['id']} cancelled")
        if time.monotonic() > self.deadline:
            raise AttemptTimeout(f"attempt exceeded timeout_s={self.job['timeout_s']}")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self.check()
            time.sleep(min(0.05, left))


def freeze_self(seconds: float) -> None:
    """Stop this whole process (every thread) for `seconds`, like a long GC pause
    or a VM stall. A detached shell sends SIGCONT to our own PID afterwards."""
    pid = os.getpid()
    subprocess.Popen(["/bin/sh", "-c", f"sleep {float(seconds)}; kill -CONT {pid}"], start_new_session=True)
    os.kill(pid, signal.SIGSTOP)


class Worker:
    def __init__(self, worker_id: str | None = None, dsn: str | None = None, port: int | None = None):
        self.cfg = settings()
        self.worker_id = worker_id or f"{socket.gethostname().split('.')[0]}-{os.getpid()}"
        self.dsn = dsn
        self.port = port
        self.conn = connect(dsn)
        self.leases = LeaseManager(self.conn, self.worker_id, self.cfg.lease_ttl_s)
        self.log = JsonLog("worker", worker=self.worker_id)
        self.stop = threading.Event()
        self.current: JobContext | None = None
        self.last_loop = time.monotonic()
        self._hb_thread: threading.Thread | None = None

    # ---- registry + heartbeats -------------------------------------------------
    def register(self) -> None:
        self.conn.execute(
            """INSERT INTO workers (worker_id, pid, host, port, status) VALUES (%s, %s, %s, %s, 'up')
               ON CONFLICT (worker_id) DO UPDATE SET pid = EXCLUDED.pid, host = EXCLUDED.host,
                   port = EXCLUDED.port, started_at = now(), last_seen = now(), status = 'up', current_job = NULL""",
            (self.worker_id, os.getpid(), socket.gethostname(), self.port),
        )

    def start_heartbeats(self) -> None:
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        # Own connection: the main thread may be inside a long statement.
        hb_conn = connect(self.dsn)
        leases = LeaseManager(hb_conn, self.worker_id, self.cfg.lease_ttl_s)
        while not self.stop.is_set():
            ctx = self.current
            try:
                hb_conn.execute("UPDATE workers SET last_seen = now(), current_job = %s WHERE worker_id = %s",
                                (ctx.job["id"] if ctx else None, self.worker_id))
                if ctx is not None and not ctx.lease_lost.is_set():
                    row = leases.heartbeat(ctx.job)
                    if row["cancel_requested"]:
                        ctx.cancelled.set()
            except LeaseLost as exc:
                ctx.lease_lost.set()
                self.log.warn("lease lost", job_id=ctx.job["id"], error=str(exc))
            except Exception as exc:  # a DB blip must not kill the heartbeat thread
                self.log.error("heartbeat error", error=repr(exc))
            self.stop.wait(self.cfg.heartbeat_s)

    # ---- the loop ----------------------------------------------------------------
    def run_once(self) -> bool:
        self.last_loop = time.monotonic()
        for r in self.leases.reap_expired():
            self.log.warn("reclaimed expired lease", job_id=r["id"], to=r["state"])
        job = self.leases.claim()
        if job is None:
            return False
        self.log.info("claimed", job_id=job["id"], type=job["type"], fencing_token=job["fencing_token"])
        self.execute(job)
        return True

    def _failpoint(self, job: dict, at: str) -> None:
        """Deterministic fault injection from the job payload (used by /learn and tests):
        {"_failpoint": {"at": "before_commit", "attempt": 1, "freeze_s": 5.5}} or {"kill": true}."""
        fp = (job.get("payload") or {}).get("_failpoint")
        if not fp or fp.get("at") != at or int(fp.get("attempt", 1)) != job["attempts"]:
            return
        if fp.get("kill"):
            self.log.warn("failpoint: SIGKILL self", job_id=job["id"], at=at)
            os.kill(os.getpid(), signal.SIGKILL)
        if fp.get("freeze_s"):
            self.log.warn("failpoint: SIGSTOP self", job_id=job["id"], at=at, seconds=fp["freeze_s"])
            store.add_event(self.conn, job["id"], "worker_frozen", self.worker_id,
                            detail={"pid": os.getpid(), "seconds": fp["freeze_s"], "at": at})
            freeze_self(float(fp["freeze_s"]))
            self.log.warn("resumed after SIGSTOP", job_id=job["id"])

    def execute(self, job: dict) -> None:
        job = self.leases.start(job)
        if job is None:
            return
        ctx = JobContext(job, self, time.monotonic() + job["timeout_s"])
        self.current = ctx
        try:
            self._failpoint(job, "after_start")
            try:
                result = jobtypes.build(job["type"], job["payload"]).run(ctx)
            except LeaseLost:
                self.log.warn("abandoning job: lease lost", job_id=job["id"])
                return
            except jobtypes.FencedOut as exc:
                self.leases.record_stale(job, "integration_write", error=str(exc))
                self.log.warn("fenced out by integration", job_id=job["id"], error=str(exc))
                return
            except Cancelled:
                self.leases.finish(job, State.CANCELLED, error="cancel requested", outcome="cancelled")
                return
            except Exception as exc:
                self.on_failure(job, exc)
                return
            self._failpoint(job, "before_commit")
            if self.leases.finish(job, State.SUCCEEDED, result=result, outcome="succeeded") is None:
                self.log.warn("result write rejected: stale fencing token", job_id=job["id"],
                              fencing_token=job["fencing_token"])
            else:
                self.log.info("succeeded", job_id=job["id"], fencing_token=job["fencing_token"])
        finally:
            self.current = None

    def on_failure(self, job: dict, exc: Exception) -> None:
        self.leases.finish(job, State.FAILED_TERMINAL, error=str(exc), outcome="failed")
        self.log.warn("failed", job_id=job["id"], error=str(exc))

    def run_forever(self, drain_after_s: float | None = None) -> None:
        self.register()
        self.start_heartbeats()
        self.log.info("worker started", port=self.port, lease_ttl_s=self.cfg.lease_ttl_s)
        idle_since = time.monotonic()
        try:
            while not self.stop.is_set():
                if self.run_once():
                    idle_since = time.monotonic()
                elif drain_after_s is not None and time.monotonic() - idle_since > drain_after_s:
                    self.log.info("drained, exiting")
                    return
                else:
                    self.stop.wait(self.cfg.poll_s)
        finally:
            self.stop.set()
            try:
                self.conn.execute("UPDATE workers SET status = 'stopped', current_job = NULL WHERE worker_id = %s",
                                  (self.worker_id,))
            except Exception:
                pass


# ---- health / admin HTTP --------------------------------------------------------
def health_server(worker: Worker, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, ctype="application/json"):
            data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":  # liveness: the process answers
                return self._send(200, {"status": "alive", "worker": worker.worker_id, "pid": os.getpid()})
            if self.path == "/readyz":  # readiness: DB reachable and the loop is turning
                stale = time.monotonic() - worker.last_loop
                try:
                    with connect(worker.dsn, connect_timeout=2) as c:
                        c.execute("SELECT 1")
                except Exception as exc:
                    return self._send(503, {"status": "not ready", "error": str(exc)})
                busy = worker.current is not None
                if not busy and stale > 10:
                    return self._send(503, {"status": "not ready", "error": f"loop idle {stale:.1f}s"})
                return self._send(200, {"status": "ready", "busy": busy})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/admin/kill":
                self._send(202, {"killing": os.getpid()})
                threading.Timer(0.1, os.kill, (os.getpid(), signal.SIGKILL)).start()
                return
            if self.path.startswith("/admin/freeze"):
                secs = float(self.path.partition("seconds=")[2] or 6)
                self._send(202, {"freezing": os.getpid(), "seconds": secs})
                threading.Timer(0.1, freeze_self, (secs,)).start()
                return
            return self._send(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="health", daemon=True).start()
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--port", type=int, default=None, help="health/admin port (8911-8914 in the demo)")
    ap.add_argument("--drain", type=float, default=None, help="exit after N idle seconds")
    args = ap.parse_args()
    w = Worker(args.id, port=args.port)
    if args.port:
        health_server(w, args.port)
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    w.run_forever(args.drain)


if __name__ == "__main__":
    main()
