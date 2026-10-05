"""Performance check: after 100k readings for one device, 100-row pure-append
batches must respond with P95 <= 1s.

Usage: python -m scripts.bench [n_batches]
"""
from __future__ import annotations

import os
import statistics
import sys
import time

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@/vibration_bench?host=/tmp")

import psycopg  # noqa: E402
from urllib.parse import urlsplit, urlunsplit  # noqa: E402

from app import service  # noqa: E402
from app.db import get_pool, init_schema  # noqa: E402
from app.models import ReadingIn  # noqa: E402

N_SEED = 100_000
BATCH = 100
BASE = 1_700_000_000


def _ensure_db():
    parts = urlsplit(os.environ["DATABASE_URL"])
    admin = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname='vibration_bench'")
        if cur.fetchone() is None:
            cur.execute("CREATE DATABASE vibration_bench")


def main(n_batches: int = 50) -> None:
    _ensure_db()
    init_schema()
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute("TRUNCATE alert_changes, alerts, readings, devices RESTART IDENTITY")
        conn.commit()

    dev = "bench-100k"
    t0 = time.perf_counter()
    # Bulk seed directly: benchmark measures pure-append after 100k exist,
    # not the cost of seeding through the correction path.
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO devices(device_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (dev,),
        )
        with conn.cursor().copy(
            "COPY readings (device_id, sample_id, rev, ts, amplitude, "
            "baseline, window_count, is_hit) FROM STDIN"
        ) as copy:
            for i in range(N_SEED):
                # ts>=20 all have a known baseline of 1 from 20+ prior samples
                copy.write_row((dev, f"seed-{i}", 1, BASE + i, 1.0,
                                1.0 if i >= 20 else None,
                                min(i, 3600), False))
        conn.commit()
    seed_s = time.perf_counter() - t0
    print(f"seeded {N_SEED} readings in {seed_s:.1f}s")

    lat_ms: list[float] = []
    t = BASE + N_SEED
    for b in range(n_batches):
        chunk = [
            ReadingIn(sample_id=f"new-{b}-{i}", ts=t + b * BATCH + i,
                      rev=1, amplitude=1.0)
            for i in range(BATCH)
        ]
        s = time.perf_counter()
        service.ingest(dev, chunk)
        lat_ms.append((time.perf_counter() - s) * 1000)

    p50 = statistics.median(lat_ms)
    p95 = statistics.quantiles(lat_ms, n=20)[18]  # 95th percentile
    print(f"batches={len(lat_ms)} batch_size={BATCH}")
    print(f"p50={p50:.1f}ms  p95={p95:.1f}ms  max={max(lat_ms):.1f}ms")
    print("PASS" if p95 <= 1000 else "FAIL: p95 exceeds 1s")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 50)
