"""Runtime settings, all from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_DSN = "postgresql://leasejobs:leasejobs-dev@127.0.0.1:54311/leasejobs"


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def sabotage() -> frozenset[str]:
    """Protections switched off on purpose, e.g. LEASEJOBS_LAB_SABOTAGE=no_fencing.

    Read on every call so a test can flip it for one subprocess. Known values:
    no_reclaim, no_fencing, no_skip_locked. Anything else is rejected loudly so a
    typo can't silently leave the protection on and fake a green control run.
    """
    raw = os.environ.get("LEASEJOBS_LAB_SABOTAGE", "")
    names = frozenset(p.strip() for p in raw.split(",") if p.strip())
    unknown = names - {"no_reclaim", "no_fencing", "no_skip_locked"}
    if unknown:
        raise RuntimeError(f"unknown LEASEJOBS_LAB_SABOTAGE values: {sorted(unknown)}")
    return names


@dataclass(frozen=True)
class Settings:
    dsn: str = field(default_factory=lambda: os.environ.get("LEASEJOBS_DSN", DEFAULT_DSN))
    lease_ttl_s: float = field(default_factory=lambda: _float("LEASEJOBS_LEASE_TTL_S", 4.0))
    heartbeat_s: float = field(default_factory=lambda: _float("LEASEJOBS_HEARTBEAT_S", 1.0))
    poll_s: float = field(default_factory=lambda: _float("LEASEJOBS_POLL_S", 0.25))
    integration_url: str = field(
        default_factory=lambda: os.environ.get("LEASEJOBS_INTEGRATION_URL", "http://127.0.0.1:8915")
    )


def settings() -> Settings:
    return Settings()
