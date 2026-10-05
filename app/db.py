"""PostgreSQL connection pool and schema bootstrap."""
from __future__ import annotations

import os
from pathlib import Path

from psycopg_pool import ConnectionPool

DSN = os.getenv("DATABASE_URL", "postgresql://postgres@/vibration")
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_pool: ConnectionPool | None = None


def _configure(conn) -> None:
    # JIT helps only for large analytical queries and needs LLVM shared
    # libraries that minimal images may not ship; disable it per connection.
    prev = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute("SET jit = off")
    finally:
        conn.autocommit = prev


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DSN,
            min_size=int(os.getenv("DB_POOL_MIN", "2")),
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
            kwargs={"autocommit": False},
            configure=_configure,
            open=True,
        )
    return _pool


def init_schema() -> None:
    pool = get_pool()
    ddl = SCHEMA_PATH.read_text()
    with pool.connection() as conn:
        conn.execute(ddl)
        conn.commit()
