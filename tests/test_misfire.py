from datetime import datetime, timedelta, timezone

from leasejobs.schedules import due_slots, step_fn

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
MIN = step_fn(None, 60)


def m(n):
    return T0 + timedelta(minutes=n)


def test_not_due_yet():
    assert due_slots(m(5), m(4), MIN, "skip", 10) == ([], m(5))


def test_on_time_fires_one_slot():
    assert due_slots(m(5), m(5), MIN, "skip", 10) == ([m(5)], m(6))


def test_skip_collapses_missed_runs_into_the_latest_one():
    # scheduler was down from minute 0 to minute 42:30
    slots, nxt = due_slots(m(0), m(42.5), MIN, "skip", 10)
    assert slots == [m(42)] and nxt == m(43)


def test_catch_up_replays_missed_runs_up_to_the_cap():
    slots, nxt = due_slots(m(0), m(42.5), MIN, "catch_up", 5)
    assert slots == [m(38), m(39), m(40), m(41), m(42)] and nxt == m(43)
    slots, _ = due_slots(m(0), m(2.5), MIN, "catch_up", 5)
    assert slots == [m(0), m(1), m(2)]


def test_cron_schedule_uses_the_same_rules():
    hourly = step_fn("0 * * * *", None)
    slots, nxt = due_slots(T0, T0 + timedelta(hours=3, minutes=10), hourly, "skip", 10)
    assert slots == [T0 + timedelta(hours=3)] and nxt == T0 + timedelta(hours=4)
