# The first version of the rate-limit deferral in leasejobs/worker.py
# (kept here for docs/problems.md; not imported anywhere).
# @learn:legacy-defer
g = self.limiter.try_acquire(name)
if not g.ok:
    wait = g.wait_s * (1 + random.random() * 0.2)  # small jitter so deferred jobs don't stampede back
    self.leases.defer(job, wait, {"integration": name, "tokens_left": g.tokens_left})
    return False
# @learn:end legacy-defer
