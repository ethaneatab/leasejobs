# First version of the "no limit configured" branch in leasejobs/ratelimit.py
# (kept for docs/problems.md; not imported anywhere).
# @learn:legacy-infinity
if row is None:  # not configured: no limit
    return Grant(True, name, float("inf"), float("inf"), 0.0)
# ... later, in the worker:
store.add_event(self.conn, job["id"], "rate_token", self.worker_id,
                detail={"integration": name, "tokens_left": g.tokens_left, "capacity": g.capacity})
# @learn:end legacy-infinity
