"""Prometheus metrics.

Two kinds:
* Process counters (worker / scheduler), on the default registry: what this
  process did. Failure rate = rate of non-succeeded attempts over all attempts.
* Queue gauges computed from Postgres at scrape time by the API (one place,
  so N workers don't each report the same backlog): backlog by state, the age
  of the oldest runnable job, and stalled jobs by reason.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

ATTEMPTS = Counter("leasejobs_attempts_total", "Finished attempts by outcome", ["type", "outcome"])
ATTEMPT_SECONDS = Histogram("leasejobs_attempt_duration_seconds", "Attempt wall time", ["type"],
                            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300))
RECLAIMED = Counter("leasejobs_leases_reclaimed_total", "Expired leases this worker took back")
LEASE_LOST = Counter("leasejobs_lease_lost_total", "Times this worker found its lease gone")
STALE_REJECTED = Counter("leasejobs_stale_writes_rejected_total", "Fenced writes that bounced", ["op"])
RATE_LIMITED = Counter("leasejobs_rate_limited_total", "Jobs handed back for lack of a token", ["integration"])
RELEASED = Counter("leasejobs_scheduler_released_total", "Delayed jobs moved to PENDING")
FIRED = Counter("leasejobs_scheduler_fired_jobs_total", "Jobs created by recurring schedules")
HEAP_SIZE = Gauge("leasejobs_scheduler_heap_size", "Entries in the scheduler heap")


# @learn:stalled-sql
STALLED_SQL = """
SELECT 'lease_expired_unreclaimed' AS reason, id, state, lease_owner,
       extract(epoch FROM now() - lease_expires_at) AS seconds
  FROM jobs WHERE state IN ('LEASED', 'RUNNING') AND lease_expires_at < now() - make_interval(secs => %(grace)s)
UNION ALL
SELECT 'running_over_timeout', j.id, j.state, j.lease_owner,
       extract(epoch FROM now() - a.started_at) - j.timeout_s
  FROM jobs j JOIN job_attempts a ON a.job_id = j.id AND a.attempt_no = j.attempts AND a.outcome = 'running'
 WHERE j.state = 'RUNNING' AND a.started_at < now() - make_interval(secs => j.timeout_s + %(grace)s)
UNION ALL
SELECT 'scheduled_overdue', id, state, NULL, extract(epoch FROM now() - run_at)
  FROM jobs WHERE state = 'SCHEDULED' AND run_at < now() - make_interval(secs => %(sched_grace)s)
ORDER BY seconds DESC LIMIT 200
"""
# @learn:end stalled-sql


def stalled(conn, lease_ttl_s: float) -> list[dict]:
    """Jobs that should have moved by now. Each reason points at a different broken part:
    lease_expired_unreclaimed -> no live worker is reaping; running_over_timeout -> a worker
    is heartbeating but not enforcing its deadline; scheduled_overdue -> no scheduler leader."""
    return conn.execute(STALLED_SQL, {"grace": 2 * lease_ttl_s, "sched_grace": 30}).fetchall()


class QueueCollector(Collector):
    def __init__(self, pool, lease_ttl_s: float, cache=None):
        self.pool, self.ttl, self.cache = pool, lease_ttl_s, cache

    def collect(self):
        backlog = GaugeMetricFamily("leasejobs_jobs", "Jobs by state", labels=["state"])
        oldest = GaugeMetricFamily("leasejobs_oldest_runnable_age_seconds",
                                   "How long the oldest PENDING job has been runnable")
        stalled_g = GaugeMetricFamily("leasejobs_stalled_jobs", "Jobs that should have moved", labels=["reason"])
        attempts = GaugeMetricFamily("leasejobs_job_attempts", "Attempt rows by outcome", labels=["outcome"])
        alive = GaugeMetricFamily("leasejobs_workers_alive", "Workers seen in the last 3 s")
        with self.pool.connection() as c:
            counts = {r["state"]: r["n"] for r in c.execute("SELECT state, count(*) AS n FROM jobs GROUP BY 1")}
            for st in ("SCHEDULED", "PENDING", "LEASED", "RUNNING", "SUCCEEDED", "FAILED_RETRYABLE",
                       "FAILED_TERMINAL", "TIMED_OUT", "CANCELLED"):
                backlog.add_metric([st], counts.get(st, 0))
            age = c.execute("SELECT coalesce(extract(epoch FROM now() - min(run_at)), 0) AS a FROM jobs "
                            "WHERE state = 'PENDING'").fetchone()["a"]
            oldest.add_metric([], float(age))
            by_reason = {"lease_expired_unreclaimed": 0, "running_over_timeout": 0, "scheduled_overdue": 0}
            for r in stalled(c, self.ttl):
                by_reason[r["reason"]] += 1
            for k, v in by_reason.items():
                stalled_g.add_metric([k], v)
            for r in c.execute("SELECT outcome, count(*) AS n FROM job_attempts GROUP BY 1"):
                attempts.add_metric([r["outcome"]], r["n"])
            alive.add_metric([], c.execute("SELECT count(*) AS n FROM workers WHERE status = 'up' AND "
                                           "last_seen > now() - interval '3 seconds'").fetchone()["n"])
        yield from (backlog, oldest, stalled_g, attempts, alive)
        if self.cache is not None:
            st = self.cache.stats()
            for name in ("hits", "misses", "evictions", "size"):
                g = GaugeMetricFamily(f"leasejobs_result_cache_{name}", f"Result cache {name}")
                g.add_metric([], st[name])
                yield g
