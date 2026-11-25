"""Tests run against a real Postgres (docker-compose -p leasejobs up -d postgres,
or the CI service container). Each session recreates database leasejobs_test;
each test starts from empty tables."""
from __future__ import annotations

import os

import psycopg
import pytest

from leasejobs.db import connect
from leasejobs.migrate import migrate
from leasejobs.seed import seed

ADMIN_DSN = os.environ.get("LEASEJOBS_DSN", "postgresql://leasejobs:leasejobs-dev@127.0.0.1:54311/leasejobs")
TEST_DSN = os.environ.get("LEASEJOBS_TEST_DSN", ADMIN_DSN.rsplit("/", 1)[0] + "/leasejobs_test")
KEEP = {"schema_migrations", "job_transitions"}


@pytest.fixture(scope="session")
def dsn() -> str:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS leasejobs_test WITH (FORCE)")
        c.execute("CREATE DATABASE leasejobs_test")
    migrate(TEST_DSN)
    os.environ["LEASEJOBS_DSN"] = TEST_DSN  # code under test, and its subprocesses, use the test DB
    return TEST_DSN


@pytest.fixture()
def conn(dsn):
    with connect(dsn) as c:
        tables = [r["tablename"] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'") if r["tablename"] not in KEEP]
        c.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
        seed(dsn)
        yield c


ALICE = {"X-API-Key": "dev-key-alice"}
BOB = {"X-API-Key": "dev-key-bob"}


@pytest.fixture()
def client(conn, dsn):
    from fastapi.testclient import TestClient

    from leasejobs.api import create_app
    with TestClient(create_app(dsn)) as c:
        yield c
