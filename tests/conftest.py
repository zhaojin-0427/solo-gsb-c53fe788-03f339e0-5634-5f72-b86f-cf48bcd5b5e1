"""Pytest fixtures: isolated PostgreSQL-backed app per test session."""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@/vibration_test?host=/tmp")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    # Create the test database if it does not exist yet.
    import psycopg
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    admin_parts = parts._replace(path="/postgres")
    admin_url = urlunsplit(admin_parts)
    with psycopg.connect(admin_url, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname='vibration_test'")
            if cur.fetchone() is None:
                cur.execute("CREATE DATABASE vibration_test")

    db.init_schema()
    yield
    db.get_pool().close()


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    pool = db.get_pool()
    with pool.connection() as conn:
        conn.execute("TRUNCATE alert_changes, alerts, readings, devices RESTART IDENTITY")
        conn.commit()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def dev():
    return f"dev-{time.time_ns()}"
