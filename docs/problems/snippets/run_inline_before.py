# How the worker ran a job before step 7 (kept for docs/problems.md).
# The deadline only works if the job itself calls ctx.check().
# @learn:legacy-run-inline
ctx = JobContext(job, self, time.monotonic() + job["timeout_s"])
try:
    result = jobtypes.build(job["type"], job["payload"]).run(ctx)
except AttemptTimeout as exc:
    self.on_failure(job, exc)
    return
# @learn:end legacy-run-inline
