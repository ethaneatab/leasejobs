# First version of the test driver in tests/test_failures.py (kept for docs/problems.md).
# @learn:legacy-backoff-driver
for _ in range(max_rounds):
    w.run_once()
    if store.get_job(conn, jid)["state"] in TERMINAL:
        return store.get_job(conn, jid)
    s.resync()
    s.tick(now=time.time() + 3600)   # "release retries early" with a fake future clock
# @learn:end legacy-backoff-driver
