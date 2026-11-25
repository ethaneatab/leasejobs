"""Apply sql/NNN_*.sql in order, once each. `python -m leasejobs.migrate`."""
from __future__ import annotations

import sys
from pathlib import Path

from .db import connect

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def migrate(dsn: str | None = None) -> list[str]:
    applied: list[str] = []
    with connect(dsn) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        # One migrator at a time, even if demo.sh and a test start together.
        conn.execute("SELECT pg_advisory_lock(7310001)")
        try:
            done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(7310001)")
    return applied


if __name__ == "__main__":
    names = migrate(sys.argv[1] if len(sys.argv) > 1 else None)
    print("applied:", ", ".join(names) if names else "nothing (up to date)")
