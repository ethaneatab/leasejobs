"""Small helpers around psycopg 3. Every caller gets dict rows and autocommit
unless it opens an explicit transaction with `with conn.transaction():`."""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings

__all__ = ["connect", "Jsonb"]


def connect(dsn: str | None = None, **kw) -> psycopg.Connection:
    return psycopg.connect(dsn or settings().dsn, autocommit=True, row_factory=dict_row, **kw)
