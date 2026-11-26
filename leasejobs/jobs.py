"""Job types. Each one is a subclass of Job with a validated payload and a
`run(ctx)` that returns a JSON-able result or raises one of the errors below."""
from __future__ import annotations

import hashlib
import random
import statistics
import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from .config import sabotage


class JobError(Exception):
    retryable: ClassVar[bool] = True


class RetryableError(JobError):
    """The dependency might recover: try again later if attempts remain."""


class TerminalError(JobError):
    """Retrying cannot help (bad input, permanent 4xx)."""
    retryable = False


class FencedOut(JobError):
    """The downstream refused our fencing token: a newer lease holder exists."""
    retryable = False


class Job(ABC):
    type_name: ClassVar[str]
    registry: ClassVar[dict[str, type["Job"]]] = {}

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if getattr(cls, "type_name", None):
            Job.registry[cls.type_name] = cls

    @classmethod
    def validate(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the cleaned payload or raise ValueError with a readable message."""
        return payload

    def integration(self) -> str | None:
        """Name of the rate-limited external integration this job calls, if any."""
        return None

    @abstractmethod
    def run(self, ctx) -> dict[str, Any]: ...


def _num(payload: dict, key: str, default: float, lo: float, hi: float) -> float:
    v = payload.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"{key} must be a number in [{lo}, {hi}]")
    return v


class Noop(Job):
    type_name = "noop"

    def run(self, ctx):
        return {"ok": True}


class Sleep(Job):
    """Sleeps in small steps so timeouts, cancels and lost leases are noticed."""
    type_name = "sleep"

    @classmethod
    def validate(cls, payload):
        _num(payload, "seconds", 1, 0, 600)
        return payload

    def run(self, ctx):
        ctx.sleep(float(self.payload.get("seconds", 1)))
        return {"slept_s": self.payload.get("seconds", 1)}


class Block(Job):
    """A badly behaved job: blocks without ever calling ctx.check().
    Exists to test that the worker's deadline does not depend on the job's manners."""
    type_name = "block"

    @classmethod
    def validate(cls, payload):
        _num(payload, "seconds", 1, 0, 60)
        return payload

    def run(self, ctx):
        time.sleep(float(self.payload.get("seconds", 1)))
        return {"blocked_s": self.payload.get("seconds", 1)}


class BuildReport(Job):
    """A small deterministic aggregation, standing in for 'generate a report'."""
    type_name = "report"

    @classmethod
    def validate(cls, payload):
        _num(payload, "rows", 1000, 1, 2_000_000)
        _num(payload, "seed", 0, 0, 2**31)
        return payload

    def run(self, ctx):
        rows, seed = int(self.payload.get("rows", 1000)), int(self.payload.get("seed", 0))
        rng = random.Random(seed)
        values = [rng.lognormvariate(3, 0.6) for _ in range(rows)]
        ctx.check()
        values.sort()
        digest = hashlib.sha256(repr(values[:50]).encode()).hexdigest()[:16]
        return {"rows": rows, "mean": round(statistics.fmean(values), 4),
                "p95": round(values[int(0.95 * (rows - 1))], 4), "digest": digest}


class Flaky(Job):
    """Fails its first `fail_times` attempts; used to exercise retries."""
    type_name = "flaky"

    @classmethod
    def validate(cls, payload):
        _num(payload, "fail_times", 1, 0, 20)
        return payload

    def run(self, ctx):
        if ctx.attempt_no <= int(self.payload.get("fail_times", 1)):
            if self.payload.get("terminal"):
                raise TerminalError(f"permanent failure on attempt {ctx.attempt_no}")
            raise RetryableError(f"transient failure on attempt {ctx.attempt_no}")
        return {"succeeded_on_attempt": ctx.attempt_no}


class Tally(Job):
    """Writes one row to `tally` per execution: a countable side effect."""
    type_name = "tally"

    def run(self, ctx):
        ctx.conn.execute("INSERT INTO tally (job_id, worker_id) VALUES (%s, %s)", (ctx.job["id"], ctx.worker_id))
        return {"tallied_by": ctx.worker_id}


class CallIntegration(Job):
    """Do some work, then write the outcome to an external integration.

    The write carries our fencing token. The integration refuses a token older
    than one it has already seen, which is what makes a revived stale worker
    harmless downstream, not just in our own database.
    """
    type_name = "call_integration"

    @classmethod
    def validate(cls, payload):
        name = payload.get("integration", "ledger-api")
        if not isinstance(name, str) or not name.replace("-", "").isalnum() or len(name) > 40:
            raise ValueError("integration must be a short name like 'ledger-api'")
        _num(payload, "work_s", 0.2, 0, 300)
        _num(payload, "amount", 1, -1e9, 1e9)
        return {"integration": name, **payload}

    def integration(self):
        return self.payload.get("integration", "ledger-api")

    # @learn:call-integration
    def run(self, ctx):
        ctx.sleep(float(self.payload.get("work_s", 0.2)))
        key = str(self.payload.get("key") or f"job-{ctx.job['id']}")
        headers = {} if "no_fencing" in sabotage() else {"X-Fencing-Token": str(ctx.fencing_token)}
        body = {"job_id": ctx.job["id"], "attempt": ctx.attempt_no, "worker": ctx.worker_id,
                "amount": self.payload.get("amount", 1)}
        try:
            r = httpx.post(f"{ctx.integration_url}/{self.integration()}/ledger/{key}", json=body,
                           headers=headers, timeout=5.0)
        except httpx.HTTPError as exc:
            raise RetryableError(f"integration unreachable: {exc.__class__.__name__}") from exc
        if r.status_code == 409:
            raise FencedOut(f"integration refused token {ctx.fencing_token}: {r.text}")
        if r.status_code >= 500:
            raise RetryableError(f"integration {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise TerminalError(f"integration {r.status_code}: {r.text[:200]}")
        return {"ledger_key": key, "accepted_token": r.json().get("token"), "worker": ctx.worker_id,
                "attempt": ctx.attempt_no}
    # @learn:end call-integration


def build(job_type: str, payload: dict[str, Any]) -> Job:
    cls = Job.registry.get(job_type)
    if cls is None:
        raise ValueError(f"unknown job type {job_type!r}; known: {sorted(Job.registry)}")
    return cls(cls.validate(payload))


def known_types() -> list[str]:
    return sorted(Job.registry)

