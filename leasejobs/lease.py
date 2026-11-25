"""LeaseManager: how a worker takes a job, keeps it, loses it, and finishes it.

A lease is (lease_owner, lease_expires_at, fencing_token) on the job row.
Times come from the database clock, so workers never have to agree on wall
time. Every write a worker makes about a job it holds is a *fenced write*: it
only lands if the row still carries the worker's own fencing token.
"""
from __future__ import annotations

from typing import Any

import psycopg

from . import store
from .config import sabotage
from .db import Jsonb
from .states import State, check


class LeaseLost(Exception):
    """Our lease is gone: it expired and someone else may hold the job now."""


class LeaseManager:
    def __init__(self, conn: psycopg.Connection, worker_id: str, ttl_s: float = 4.0):
        self.conn = conn
        self.worker_id = worker_id
        self.ttl_s = ttl_s

    # @learn:claim
    def claim(self) -> dict | None:
        """Take the best runnable job in one statement, skipping rows other workers hold.

        The inner SELECT ... FOR UPDATE SKIP LOCKED is the whole trick: a row
        another worker is in the middle of claiming is invisible to us instead of
        making us wait for it, so N workers spread over N different rows.
        Each claim bumps fencing_token, so every lease ever granted on a job has
        a bigger number than the one before it.
        """
        check(State.PENDING, State.LEASED)
        lock = "FOR UPDATE" if "no_skip_locked" in sabotage() else "FOR UPDATE SKIP LOCKED"
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                UPDATE jobs SET state = 'LEASED', lease_owner = %(me)s, leased_at = now(),
                       lease_expires_at = now() + make_interval(secs => %(ttl)s),
                       fencing_token = fencing_token + 1
                WHERE id = (SELECT id FROM jobs
                            WHERE state = 'PENDING' AND run_at <= now()
                            ORDER BY priority DESC, run_at, id
                            LIMIT 1 {lock})
                RETURNING *""",
                {"me": self.worker_id, "ttl": self.ttl_s},
            ).fetchone()
            if row is not None:
                store.add_event(self.conn, row["id"], "claim", self.worker_id, frm="PENDING", to="LEASED",
                                detail={"lease_owner": self.worker_id, "fencing_token": row["fencing_token"],
                                        "lease_expires_at": row["lease_expires_at"].isoformat()})
        return row
    # @learn:end claim

    # @learn:fence
    def fence(self, job: dict) -> tuple[str, tuple]:
        """The guard every write about a held job carries."""
        if "no_fencing" in sabotage():
            # LAB: trust whoever shows up. A worker that was presumed dead can
            # come back and overwrite the result of the worker that replaced it.
            return "TRUE", ()
        return "lease_owner = %s AND fencing_token = %s", (self.worker_id, job["fencing_token"])
    # @learn:end fence

    # @learn:heartbeat
    def heartbeat(self, job: dict) -> dict:
        """Push our lease forward. Raises LeaseLost if the row is no longer ours."""
        where, params = self.fence(job)
        row = self.conn.execute(
            f"""UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now()
                WHERE id = %s AND state IN ('LEASED', 'RUNNING') AND {where}
                RETURNING lease_expires_at, cancel_requested, fencing_token""",
            (self.ttl_s, job["id"], *params),
        ).fetchone()
        if row is None:
            self.record_stale(job, "heartbeat")
            raise LeaseLost(f"job {job['id']}: lease with token {job['fencing_token']} is gone")
        store.add_event(self.conn, job["id"], "heartbeat", self.worker_id,
                        detail={"fencing_token": job["fencing_token"],
                                "lease_expires_at": row["lease_expires_at"].isoformat()})
        return row
    # @learn:end heartbeat

    # @learn:reap
    def reap_expired(self, limit: int = 10) -> list[dict]:
        """Hand back jobs whose lease ran out, so any worker can claim them again.

        LEASED/RUNNING with lease_expires_at in the past means the holder stopped
        heartbeating: it crashed, hung, or got partitioned away. We can't tell
        which, and we don't need to: the fencing token makes its late writes
        harmless. A job that expired on its last allowed attempt is failed for
        good instead of being retried forever.
        """
        if "no_reclaim" in sabotage():
            return []  # LAB: nobody ever takes an expired lease back
        reaped = []
        with self.conn.transaction():
            rows = self.conn.execute(
                """SELECT id, state, lease_owner, fencing_token, lease_expires_at, attempts, max_attempts,
                          extract(epoch FROM now() - lease_expires_at) * 1000 AS overdue_ms
                   FROM jobs WHERE state IN ('LEASED', 'RUNNING') AND lease_expires_at < now()
                   ORDER BY lease_expires_at LIMIT %s FOR UPDATE SKIP LOCKED""",
                (limit,),
            ).fetchall()
            for r in rows:
                last = r["state"] == "RUNNING" and r["attempts"] >= r["max_attempts"]
                to = State.FAILED_TERMINAL if last else State.PENDING
                detail = {"expired_owner": r["lease_owner"], "fencing_token": r["fencing_token"],
                          "lease_expires_at": r["lease_expires_at"].isoformat(),
                          "overdue_ms": round(float(r["overdue_ms"]))}
                extra = ({"finished_at": store.Raw("now()"), "last_error": "lease expired on the last attempt"}
                         if last else {})
                row = store.transition(
                    self.conn, r["id"], State(r["state"]), to, actor=self.worker_id, kind="lease_expired",
                    set_={"lease_owner": None, "lease_expires_at": None, **extra},
                    guard=("fencing_token = %s", (r["fencing_token"],)), detail=detail)
                if row is not None:
                    self.conn.execute(
                        "UPDATE job_attempts SET outcome = 'lease_expired', finished_at = now() "
                        "WHERE job_id = %s AND attempt_no = %s AND outcome = 'running'",
                        (r["id"], r["attempts"]))
                    reaped.append(row)
        return reaped
    # @learn:end reap

    def start(self, job: dict) -> dict | None:
        where, params = self.fence(job)
        row = store.transition(self.conn, job["id"], State.LEASED, State.RUNNING, actor=self.worker_id,
                               kind="start", set_={"attempts": store.Raw("attempts + 1")},
                               guard=(where, params), detail={"fencing_token": job["fencing_token"]})
        if row is None:
            self.record_stale(job, "start")
            return None
        self.conn.execute(
            "INSERT INTO job_attempts (job_id, attempt_no, worker_id, fencing_token) VALUES (%s, %s, %s, %s)",
            (row["id"], row["attempts"], self.worker_id, job["fencing_token"]),
        )
        return row

    # @learn:finish
    def finish(self, job: dict, to: State, *, result: dict[str, Any] | None = None,
               error: str | None = None, outcome: str, set_: dict | None = None) -> dict | None:
        """Record the end of our attempt. Returns None if our lease is no longer current."""
        where, params = self.fence(job)
        cols = {"lease_owner": None, "lease_expires_at": None, "last_error": error, **(set_ or {})}
        if result is not None:
            cols["result"] = Jsonb(result)
        if to in (State.SUCCEEDED, State.FAILED_TERMINAL, State.TIMED_OUT, State.CANCELLED):
            cols["finished_at"] = store.Raw("now()")
        row = store.transition(
            self.conn, job["id"], State.RUNNING, to, actor=self.worker_id, kind="finish", set_=cols,
            guard=(where, params),
            detail={"outcome": outcome, "fencing_token": job["fencing_token"], **({"error": error} if error else {})},
        )
        if row is None:
            self.record_stale(job, "finish", attempted=to.value)
            return None
        self.conn.execute(
            "UPDATE job_attempts SET finished_at = now(), outcome = %s, error = %s "
            "WHERE job_id = %s AND attempt_no = %s",
            (outcome, error, job["id"], job["attempts"]),
        )
        return row
    # @learn:end finish

    def record_stale(self, job: dict, op: str, **extra) -> None:
        """A fenced write bounced. Write down who tried and who holds the job now."""
        now = self.conn.execute("SELECT state, lease_owner, fencing_token FROM jobs WHERE id = %s",
                                (job["id"],)).fetchone()
        store.add_event(self.conn, job["id"], "stale_write_rejected", self.worker_id,
                        detail={"op": op, "my_token": job["fencing_token"], "current_token": now["fencing_token"],
                                "current_owner": now["lease_owner"], "current_state": now["state"], **extra})
