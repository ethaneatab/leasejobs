"""Job rows, attempts and events. All state changes go through `transition`."""
from __future__ import annotations

from typing import Any

import psycopg

from .db import Jsonb
from .states import State, check


class _Raw:
    """A SQL expression used as a SET value, e.g. Raw('fencing_token + 1')."""

    def __init__(self, sql: str):
        self.sql = sql


Raw = _Raw


def add_event(conn: psycopg.Connection, job_id: int, kind: str, actor: str, *,
              frm: str | None = None, to: str | None = None, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO job_events (job_id, kind, from_state, to_state, actor, detail) VALUES (%s,%s,%s,%s,%s,%s)",
        (job_id, kind, frm, to, actor, Jsonb(detail or {})),
    )


# @learn:transition
def transition(conn: psycopg.Connection, job_id: int, frm: State, to: State, *, actor: str,
               kind: str = "transition", set_: dict[str, Any] | None = None,
               guard: tuple[str, tuple] | None = None, detail: dict | None = None) -> dict | None:
    """Move one job frm -> to, only if it is still in frm (and the optional guard holds).

    Returns the updated row, or None when another actor got there first. The
    caller decides what "lost the race" means; nothing is retried here.
    """
    check(frm, to)  # refuse illegal edges before talking to the database
    cols = {"state": to.value, **(set_ or {})}
    assigns = ", ".join(f"{c} = %s" if not isinstance(v, _Raw) else f"{c} = {v.sql}" for c, v in cols.items())
    params: list[Any] = [v for v in cols.values() if not isinstance(v, _Raw)]
    sql = f"UPDATE jobs SET {assigns} WHERE id = %s AND state = %s"
    params += [job_id, frm.value]
    if guard:
        sql += f" AND ({guard[0]})"
        params += list(guard[1])
    with conn.transaction():
        row = conn.execute(sql + " RETURNING *", params).fetchone()
        if row is not None:
            add_event(conn, job_id, kind, actor, frm=frm.value, to=to.value, detail=detail)
    return row
# @learn:end transition


def get_job(conn: psycopg.Connection, job_id: int, owner: str | None = None) -> dict | None:
    if owner is None:
        return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    return conn.execute("SELECT * FROM jobs WHERE id = %s AND owner = %s", (job_id, owner)).fetchone()


def list_jobs(conn: psycopg.Connection, owner: str, state: str | None, limit: int) -> list[dict]:
    if state:
        return conn.execute(
            "SELECT * FROM jobs WHERE owner = %s AND state = %s ORDER BY id DESC LIMIT %s", (owner, state, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM jobs WHERE owner = %s ORDER BY id DESC LIMIT %s", (owner, limit)).fetchall()


def attempts(conn: psycopg.Connection, job_id: int) -> list[dict]:
    return conn.execute("SELECT * FROM job_attempts WHERE job_id = %s ORDER BY attempt_no", (job_id,)).fetchall()


def events(conn: psycopg.Connection, job_id: int) -> list[dict]:
    return conn.execute("SELECT * FROM job_events WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
