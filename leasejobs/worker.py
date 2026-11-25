"""A worker process: claim one job, run it, record the outcome, repeat."""
from __future__ import annotations

import argparse
import os
import socket
import threading
import time

from . import jobs as jobtypes
from .config import settings
from .db import connect
from .lease import LeaseManager
from .logs import JsonLog
from .states import State


class AttemptTimeout(Exception):
    pass


class JobContext:
    """What a running job can see: its attempt, and a check() for deadlines."""

    def __init__(self, job: dict, worker_id: str, deadline: float):
        self.job = job
        self.attempt_no = job["attempts"]
        self.worker_id = worker_id
        self.deadline = deadline

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise AttemptTimeout(f"attempt exceeded timeout_s={self.job['timeout_s']}")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self.check()
            time.sleep(min(0.05, left))


class Worker:
    def __init__(self, worker_id: str | None = None, dsn: str | None = None):
        self.cfg = settings()
        self.worker_id = worker_id or f"{socket.gethostname().split('.')[0]}-{os.getpid()}"
        self.conn = connect(dsn)
        self.leases = LeaseManager(self.conn, self.worker_id)
        self.log = JsonLog("worker", worker=self.worker_id)
        self.stop = threading.Event()

    def run_once(self) -> bool:
        job = self.leases.claim()
        if job is None:
            return False
        self.log.info("claimed", job_id=job["id"], type=job["type"])
        self.execute(job)
        return True

    def execute(self, job: dict) -> None:
        job = self.leases.start(job)
        if job is None:
            return
        ctx = JobContext(job, self.worker_id, time.monotonic() + job["timeout_s"])
        try:
            result = jobtypes.build(job["type"], job["payload"]).run(ctx)
        except Exception as exc:  # step 1: any failure is final
            self.leases.finish(job, State.FAILED_TERMINAL, error=str(exc), outcome="failed")
            self.log.warn("failed", job_id=job["id"], error=str(exc))
            return
        self.leases.finish(job, State.SUCCEEDED, result=result, outcome="succeeded")
        self.log.info("succeeded", job_id=job["id"])

    def run_forever(self) -> None:
        self.log.info("worker started")
        while not self.stop.is_set():
            if not self.run_once():
                self.stop.wait(self.cfg.poll_s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    args = ap.parse_args()
    Worker(args.id).run_forever()


if __name__ == "__main__":
    main()
