from datetime import datetime, timezone

import pytest

from leasejobs.cron import Cron


def t(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("expr,after,want", [
    ("*/15 * * * *", "2026-03-01T10:07", "2026-03-01T10:15"),
    ("*/15 * * * *", "2026-03-01T10:15", "2026-03-01T10:30"),     # strictly after
    ("0 9 * * 1-5", "2026-11-27T09:00", "2026-11-30T09:00"),      # Friday 9:00 -> Monday
    ("30 2 1 * *", "2026-01-31T12:00", "2026-02-01T02:30"),
    ("0 0 31 * *", "2026-04-01T00:00", "2026-05-31T00:00"),       # April has no 31st
    ("0 0 29 2 *", "2026-03-01T00:00", "2028-02-29T00:00"),       # next leap day
    ("0 12 13 * 5", "2026-11-01T00:00", "2026-11-06T12:00"),      # dom OR dow: first Friday wins
    ("0 0 * * 7", "2026-11-25T00:00", "2026-11-29T00:00"),        # 7 is Sunday
    ("5 4 * * *", "2026-12-31T23:59", "2027-01-01T04:05"),
])
def test_next_after(expr, after, want):
    assert Cron.parse(expr).next_after(t(after)) == t(want)


@pytest.mark.parametrize("bad", ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *", "x * * * *"])
def test_rejects_bad_expressions(bad):
    with pytest.raises(ValueError):
        Cron.parse(bad)
