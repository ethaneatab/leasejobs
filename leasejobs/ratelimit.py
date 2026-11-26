"""Per-integration token buckets, shared by every worker through Postgres.

Why a row in Postgres and not a bucket inside each worker process: the limit
belongs to the *integration* ("ledger-api allows 2 calls/s"), not to a worker.
With one in-process bucket per worker, the real rate is N x the configured
one and silently changes every time you scale workers up or down. One shared
row keeps the limit global. The cost is one short row-locked UPDATE per
acquire, which is fine because a rate-limited call is slow anyway.

Refill is lazy: nothing ticks in the background. Each acquire computes how
many tokens accumulated since updated_at, capped at capacity, in the same
statement that takes one.

A worker that gets no token does NOT wait. The job goes back to SCHEDULED
with run_at = when a token will exist, and the worker moves on to other work.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .config import sabotage


def refill(tokens: float, capacity: float, refill_per_s: float, elapsed_s: float) -> float:
    return min(capacity, tokens + max(0.0, elapsed_s) * refill_per_s)


@dataclass(frozen=True)
class Grant:
    ok: bool
    integration: str
    tokens_left: float | None  # None = this integration has no limit configured
    capacity: float | None
    wait_s: float  # 0 when ok; otherwise how long until one token exists
    refill_per_s: float | None = None


class RateLimiter:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # @learn:bucket
    def try_acquire(self, name: str, n: float = 1.0) -> Grant:
        if "no_ratelimit" in sabotage():
            return Grant(True, name, None, None, 0.0)  # LAB: call as fast as we like
        row = self.conn.execute(
            """
            WITH b AS (
                SELECT name, capacity, refill_per_s,
                       LEAST(capacity, tokens + extract(epoch FROM now() - updated_at) * refill_per_s) AS avail
                FROM integrations WHERE name = %(name)s FOR UPDATE)
            UPDATE integrations i
               SET tokens = CASE WHEN b.avail >= %(n)s THEN b.avail - %(n)s ELSE b.avail END,
                   updated_at = now()
              FROM b WHERE i.name = b.name
            RETURNING b.avail >= %(n)s AS ok, i.tokens, i.capacity, i.refill_per_s""",
            {"name": name, "n": n},
        ).fetchone()
        if row is None:  # not configured: no limit
            return Grant(True, name, None, None, 0.0)
        wait = 0.0 if row["ok"] else (n - row["tokens"]) / row["refill_per_s"]
        return Grant(row["ok"], name, round(row["tokens"], 3), row["capacity"], round(wait, 3), row["refill_per_s"])
    # @learn:end bucket

    def snapshot(self) -> list[dict]:
        return self.conn.execute(
            """SELECT name, capacity, refill_per_s,
                      LEAST(capacity, tokens + extract(epoch FROM now() - updated_at) * refill_per_s) AS tokens_now
               FROM integrations ORDER BY name""").fetchall()
