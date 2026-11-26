"""A worker process.

Loop: reap expired leases -> claim one job -> start it -> run it while a
heartbeat thread keeps the lease alive -> record the outcome with a fenced
write. One job at a time per process; run more processes for more throughput.

`python -m leasejobs.worker --id worker-1 --port 8911`
"""
from __future__ import annotations

import argparse
import os
import random
import signal
import socket
import subprocess
import threading
import time

from . import jobs as jobtypes
from . import metrics, store
from .config import settings
from .db import connect
from .lease import LeaseLost, LeaseManager
from .logs import JsonLog
from .ratelimit import RateLimiter
from .states import State


HARD_TIMEOUT_GRACE_S = 0.5


def backoff_s(attempt: int) -> float:
    """Exponential backoff with 'equal jitter': half fixed, half random."""
    base = float(os.environ.get("LEASEJOBS_RETRY_BASE_S", "1.0"))
    cap = float(os.environ.get("LEASEJOBS_RETRY_CAP_S", "60"))
    d = min(cap, base * 2 ** (attempt - 1))
    return d / 2 + random.uniform(0, d / 2)


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
        self.limiter = RateLimiter(self.conn)
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
                metrics.LEASE_LOST.inc()
                ctx.lease_lost.set()
                self.log.warn("lease lost", job_id=ctx.job["id"], error=str(exc))
            except Exception as exc:  # a DB blip must not kill the heartbeat thread
                self.log.error("heartbeat error", error=repr(exc))
            self.stop.wait(self.cfg.heartbeat_s)

    # ---- the loop ----------------------------------------------------------------
    def run_once(self) -> bool:
        self.last_loop = time.monotonic()
        for r in self.leases.reap_expired():
            metrics.RECLAIMED.inc()
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

    # @learn:worker-token
    def acquire_token(self, job: dict) -> bool:
        """True if the job may run now; otherwise it has been handed back to the scheduler."""
        try:
            name = jobtypes.build(job["type"], job["payload"]).integration()
        except ValueError:
            return True  # a bad payload fails in run(), with a proper attempt record
        if not name:
            return True
        g = self.limiter.try_acquire(name)
        if g.ok:
            store.add_event(self.conn, job["id"], "rate_token", self.worker_id,
                            detail={"integration": name, "tokens_left": g.tokens_left, "capacity": g.capacity})
            return True
        # Everyone who was refused gets the same "one token in X s" hint. If they all
        # came back at X, one would win and the rest would bounce again. So spread
        # them over the time the bucket needs to serve the jobs already waiting.
        waiting = self.conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE state = 'SCHEDULED' AND deferrals > 0 "
            "AND payload->>'integration' = %s", (name,)).fetchone()["n"]
        wait = g.wait_s + random.uniform(0, (waiting + 1) / g.refill_per_s)
        metrics.RATE_LIMITED.labels(name).inc()
        self.leases.defer(job, wait, {"integration": name, "tokens_left": g.tokens_left, "queued_behind": waiting})
        self.log.info("rate limited, deferred", job_id=job["id"], integration=name, wait_s=round(wait, 3))
        return False
    # @learn:end worker-token

    def execute(self, job: dict) -> None:
        if not self.acquire_token(job):
            return
        job = self.leases.start(job)
        if job is None:
            return
        ctx = JobContext(job, self, time.monotonic() + job["timeout_s"])
        self.current = ctx
        t0 = time.monotonic()
        try:
            self._failpoint(job, "after_start")
            try:
                result = self.run_with_deadline(job, ctx)
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
            metrics.ATTEMPT_SECONDS.labels(job["type"]).observe(time.monotonic() - t0)

    # @learn:hard-timeout
    def run_with_deadline(self, job: dict, ctx: JobContext) -> dict:
        """Run the job body in its own thread and stop waiting at the deadline.

        Well-behaved jobs call ctx.check()/ctx.sleep() and stop by themselves.
        A job that blocks without checking can't be killed (Python threads can't
        be), so we abandon it: mark it lost, record the timeout, move on. Its
        late writes carry an old fencing token, so they bounce like any stale
        worker's would.
        """
        box: dict = {}

        def body():
            try:
                box["result"] = jobtypes.build(job["type"], job["payload"]).run(ctx)
            except BaseException as exc:  # handed to the caller below
                box["error"] = exc

        t = threading.Thread(target=body, name=f"job-{job['id']}", daemon=True)
        t.start()
        t.join(max(0.0, ctx.deadline - time.monotonic()) + HARD_TIMEOUT_GRACE_S)
        if t.is_alive():
            ctx.lease_lost.set()  # any later ctx.check() in that thread stops it
            self.log.warn("abandoned a job thread that ignored its deadline", job_id=job["id"])
            raise AttemptTimeout(f"attempt exceeded timeout_s={job['timeout_s']} (job did not stop; abandoned)")
        if "error" in box:
            raise box["error"]
        return box["result"]
    # @learn:end hard-timeout

    # @learn:on-failure
    def on_failure(self, job: dict, exc: Exception) -> None:
        """Decide: retry later, or stop for good. Timeouts and transient errors
        retry while attempts remain; permanent errors never do."""
        timed_out = isinstance(exc, AttemptTimeout)
        retryable = timed_out or getattr(exc, "retryable", True) and not isinstance(exc, ValueError)
        outcome = "timed_out" if timed_out else "failed"
        err = f"{exc.__class__.__name__}: {exc}"
        if retryable and job["attempts"] < job["max_attempts"]:
            delay = backoff_s(job["attempts"])
            with self.conn.transaction():  # both steps or neither: never stranded in FAILED_RETRYABLE
                if self.leases.finish(job, State.FAILED_RETRYABLE, error=err, outcome=outcome) is None:
                    return
                store.transition(self.conn, job["id"], State.FAILED_RETRYABLE, State.SCHEDULED,
                                 actor=self.worker_id, kind="retry_scheduled",
                                 set_={"run_at": store.Raw(f"now() + make_interval(secs => {delay:.3f})")},
                                 detail={"attempt": job["attempts"], "backoff_s": round(delay, 3)})
            self.log.warn("attempt failed, retry scheduled", job_id=job["id"], attempt=job["attempts"],
                          backoff_s=round(delay, 3), error=err)
            return
        final = State.TIMED_OUT if timed_out and retryable else State.FAILED_TERMINAL
        self.leases.finish(job, final, error=err, outcome=outcome)
        self.log.warn("job failed for good", job_id=job["id"], state=final.value, error=err)
    # @learn:end on-failure

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
def health_server(worker: Worker, port: int):
    """/healthz = the process answers (liveness). /readyz = DB reachable and the
    loop turned recently (readiness). /metrics. POST /admin/kill and
    /admin/freeze?seconds=N act on this process's own PID only."""
    from .health import serve_health

    def info():
        return {"worker": worker.worker_id, "pid": os.getpid(),
                "busy_job": worker.current.job["id"] if worker.current else None}

    def ready():
        try:
            with connect(worker.dsn, connect_timeout=2) as c:
                c.execute("SELECT 1")
        except Exception:
            return False
        return worker.current is not None or time.monotonic() - worker.last_loop < 10

    def admin(h) -> bool:
        if h.path == "/admin/kill":
            h.send(202, {"killing": os.getpid()})
            threading.Timer(0.1, os.kill, (os.getpid(), signal.SIGKILL)).start()
            return True
        if h.path.startswith("/admin/freeze"):
            secs = float(h.path.partition("seconds=")[2] or 6)
            h.send(202, {"freezing": os.getpid(), "seconds": secs})
            threading.Timer(0.1, freeze_self, (secs,)).start()
            return True
        return False

    return serve_health(port, info, ready, admin)


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
