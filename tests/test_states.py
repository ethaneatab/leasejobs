import itertools

import psycopg
import pytest

from leasejobs.states import TERMINAL, TRANSITIONS, IllegalTransition, State, check


def test_python_and_sql_edge_lists_are_identical(conn):
    sql = {(r["from_state"], r["to_state"]) for r in conn.execute("SELECT * FROM job_transitions")}
    py = {(a.value, b.value) for a, nxt in TRANSITIONS.items() for b in nxt}
    assert sql == py


@pytest.mark.parametrize("frm,to", list(itertools.product(State, State)))
def test_check_accepts_exactly_the_listed_edges(frm, to):
    if to in TRANSITIONS[frm]:
        check(frm, to)
    else:
        with pytest.raises(IllegalTransition):
            check(frm, to)


def test_terminal_states_have_no_way_out():
    assert TERMINAL == {State.SUCCEEDED, State.FAILED_TERMINAL, State.TIMED_OUT, State.CANCELLED}


def test_every_state_reachable_from_scheduled_or_pending():
    seen, todo = set(), [State.SCHEDULED, State.PENDING]
    while todo:
        s = todo.pop()
        if s not in seen:
            seen.add(s)
            todo.extend(TRANSITIONS[s])
    assert seen == set(State)


def test_database_refuses_an_illegal_raw_update(conn):
    jid = conn.execute("INSERT INTO jobs (owner, type, state) VALUES ('alice','noop','SUCCEEDED') RETURNING id"
                       ).fetchone()["id"]
    with pytest.raises(psycopg.errors.CheckViolation, match="illegal job transition SUCCEEDED -> PENDING"):
        conn.execute("UPDATE jobs SET state = 'PENDING' WHERE id = %s", (jid,))
