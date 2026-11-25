"""The job lifecycle as an explicit state machine.

The same edge list lives in sql/001_core.sql (job_transitions + a trigger), so
an illegal move is refused twice: here before any SQL is sent, and by Postgres
if some code path forgets to ask.
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    SCHEDULED = "SCHEDULED"            # waiting in the scheduler heap for run_at
    PENDING = "PENDING"                # runnable now, nobody holds it
    LEASED = "LEASED"                  # a worker holds the lease, has not started
    RUNNING = "RUNNING"                # attempt in progress, lease kept alive by heartbeats
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"  # attempt failed, another one is allowed
    FAILED_TERMINAL = "FAILED_TERMINAL"
    TIMED_OUT = "TIMED_OUT"            # last allowed attempt ran past timeout_s
    CANCELLED = "CANCELLED"


S = State

# @learn:state-machine
TRANSITIONS: dict[State, frozenset[State]] = {
    S.SCHEDULED: frozenset({S.PENDING, S.CANCELLED}),
    S.PENDING: frozenset({S.LEASED, S.CANCELLED}),
    # LEASED -> PENDING: lease expired before start. LEASED -> SCHEDULED: no
    # rate-limit token, hand the job back to the scheduler instead of waiting.
    S.LEASED: frozenset({S.RUNNING, S.PENDING, S.SCHEDULED, S.CANCELLED}),
    # RUNNING -> PENDING: lease expired mid-attempt, another worker may reclaim.
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED_RETRYABLE, S.FAILED_TERMINAL,
                          S.TIMED_OUT, S.CANCELLED, S.PENDING}),
    S.FAILED_RETRYABLE: frozenset({S.SCHEDULED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED_TERMINAL: frozenset(),
    S.TIMED_OUT: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class IllegalTransition(ValueError):
    def __init__(self, frm: State, to: State):
        super().__init__(f"illegal job transition {frm.value} -> {to.value}")
        self.frm, self.to = frm, to


def check(frm: State | str, to: State | str) -> None:
    frm, to = State(frm), State(to)
    if to not in TRANSITIONS[frm]:
        raise IllegalTransition(frm, to)
# @learn:end state-machine


def is_terminal(s: State | str) -> bool:
    return State(s) in TERMINAL
