# The obvious way to "not lose runs" after the scheduler was down
# (kept for docs/problems.md; leasejobs/schedules.py does not do this).
# @learn:legacy-misfire
def due_slots(next_run_at, now, step):
    slots = []
    while next_run_at <= now:
        slots.append(next_run_at)      # one job per missed slot, however many
        next_run_at = step(next_run_at)
    return slots, next_run_at
# @learn:end legacy-misfire
