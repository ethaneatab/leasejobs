# The first version of the cache warm-up loop in leasejobs/api.py
# (kept here for docs/problems.md; not imported anywhere).
# @learn:legacy-notify-loop
with connect(dsn) as lc:
    lc.execute("LISTEN leasejobs_succeeded")
    while not stop.is_set():
        for n in lc.notifies(timeout=0.5, stop_after=1):
            row = lc.execute("SELECT id, owner, result, finished_at FROM jobs WHERE id = %s",
                             (int(n.payload),)).fetchone()
            if row is not None:
                cache.put(row["id"], _result_view(row), source="notify")
# @learn:end legacy-notify-loop
