"""Insert the dev API keys. `python -m leasejobs.seed`. Safe to run twice.

LEASEJOBS_DEV_KEYS is "owner=key,owner=key". The defaults are throwaway
local-only values; set your own outside a laptop.
"""
from __future__ import annotations

import os

from .api import hash_key
from .db import connect

DEFAULT_DEV_KEYS = "alice=dev-key-alice,bob=dev-key-bob"


def seed(dsn: str | None = None) -> list[str]:
    pairs = [p.split("=", 1) for p in os.environ.get("LEASEJOBS_DEV_KEYS", DEFAULT_DEV_KEYS).split(",") if p]
    with connect(dsn) as conn:
        for owner, key in pairs:
            conn.execute("INSERT INTO api_keys (key_hash, owner) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                         (hash_key(key), owner))
    return [o for o, _ in pairs]


if __name__ == "__main__":
    print("seeded owners:", ", ".join(seed()))
