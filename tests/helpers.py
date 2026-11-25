"""Shared test helpers: insert jobs directly, run worker subprocesses by PID."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from leasejobs.db import Jsonb

ROOT = Path(__file__).resolve().parent.parent


def add_job(conn, type_: str, payload: dict | None = None, **cols) -> int:
    cols = {"owner": "alice", "type": type_, "payload": Jsonb(payload or {}), "state": "PENDING", **cols}
    names = ", ".join(cols)
    return conn.execute(f"INSERT INTO jobs ({names}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                        list(cols.values())).fetchone()["id"]


def add_jobs(conn, type_: str, n: int, payload: dict | None = None) -> list[int]:
    rows = conn.execute(
        "INSERT INTO jobs (owner, type, payload, state) SELECT 'alice', %s, %s, 'PENDING' "
        "FROM generate_series(1, %s) RETURNING id", (type_, Jsonb(payload or {}), n)).fetchall()
    return [r["id"] for r in rows]


class Proc:
    """A child process we started; we only ever signal it by its own PID."""

    def __init__(self, args: list[str], env: dict | None = None, log: Path | None = None):
        self.log = log
        out = open(log, "wb") if log else subprocess.DEVNULL
        self.p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env={**os.environ, **(env or {})},
                                  stdout=out, stderr=subprocess.STDOUT)
        self.pid = self.p.pid

    def signal(self, sig: int) -> None:
        os.kill(self.pid, sig)

    def kill9(self) -> None:
        self.signal(signal.SIGKILL)
        self.p.wait(10)

    def wait(self, timeout: float) -> int:
        return self.p.wait(timeout)

    def stop(self) -> None:
        if self.p.poll() is None:
            try:
                self.signal(signal.SIGCONT)  # in case a test left it stopped
                self.signal(signal.SIGTERM)
                self.p.wait(5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.p.poll() is None:
                    self.signal(signal.SIGKILL)
                    self.p.wait(5)


def worker(worker_id: str, *extra: str, env: dict | None = None, log: Path | None = None) -> Proc:
    return Proc(["-m", "leasejobs.worker", "--id", worker_id, *extra], env=env, log=log)


def wait_until(pred, timeout: float = 20.0, every: float = 0.05, msg: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {msg}")
