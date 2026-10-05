"""Core transaction logic: ingest, baseline recompute, alert merge, outbox."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException
from psycopg.rows import dict_row

from .db import get_pool
from .models import ReadingIn

WINDOW_SECONDS = 3600          # prior 60 minutes
MERGE_GAP_SECONDS = 300        # adjacent hits merge when at most 5 minutes apart
MIN_BASELINE_SAMPLES = 20


@dataclass(frozen=True)
class _Item:
    sample_id: str
    rev: int
    ts: int
    amplitude: Decimal


def _executemany(conn, sql: str, seq) -> None:
    with conn.cursor() as cur:
        cur.executemany(sql, seq)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    out: list[list[int]] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def _dedup_batch(items: list[ReadingIn]) -> dict[str, _Item]:
    """Collapse duplicate sample ids within one batch to their highest revision.

    Same sample / same revision / different content inside one batch is a
    conflict, exactly as if it came from storage. Equal content duplicates
    collapse to one item (their per-item verdicts are assembled afterwards).
    """
    best: dict[str, _Item] = {}
    for r in items:
        it = _Item(r.sample_id, r.rev, r.ts, Decimal(repr(r.amplitude)))
        cur = best.get(r.sample_id)
        if cur is None or r.rev > cur.rev:
            best[r.sample_id] = it
        elif r.rev == cur.rev and (r.ts != cur.ts or it.amplitude != cur.amplitude):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "revision_conflict",
                    "sample_id": r.sample_id,
                    "rev": r.rev,
                },
            )
    return best


def _alert_payload(row: dict) -> tuple:
    return (
        row["start_ts"],
        row["end_ts"],
        row["peak_ts"],
        Decimal(row["peak_amp"]),
        row["hit_count"],
        tuple(row["sample_ids"]),
    )


def ingest(device_id: str, items: list[ReadingIn]) -> list[dict]:
    wanted = _dedup_batch(items)
    pool = get_pool()

    with pool.connection() as conn:
        conn.row_factory = dict_row
        try:
            # Serialize concurrent writers for the same device.
            conn.execute(
                "INSERT INTO devices(device_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (device_id,),
            )
            conn.execute(
                "SELECT device_id FROM devices WHERE device_id = %s FOR UPDATE",
                (device_id,),
            )

            sids = list(wanted.keys())
            existing = {
                r["sample_id"]: r
                for r in conn.execute(
                    """
                    SELECT sample_id, rev, ts, amplitude, baseline,
                           window_count, is_hit
                      FROM readings
                     WHERE device_id = %s AND sample_id = ANY(%s)
                     FOR UPDATE
                    """,
                    (device_id, sids),
                ).fetchall()
            }

            # Alert state before this batch, for outbox diffing.
            old_alerts = {
                r["alert_id"]: _alert_payload(r)
                for r in conn.execute(
                    """
                    SELECT alert_id, start_ts, end_ts, peak_ts, peak_amp,
                           hit_count, sample_ids
                      FROM alerts WHERE device_id = %s
                    """,
                    (device_id,),
                ).fetchall()
            }

            results: dict[str, dict] = {}
            to_upsert: list[_Item] = []
            content_changes: list[tuple[str, int | None, int]] = []
            recompute_needed = False

            for sid, it in wanted.items():
                old = existing.get(sid)
                if old is not None and old["rev"] > it.rev:
                    results[sid] = {
                        "sample_id": sid,
                        "rev": it.rev,
                        "status": "ignored",
                        "ts": None,
                        "amplitude": None,
                        "baseline": None,
                        "window_count": 0,
                        "hit": None,
                    }
                    continue

                if (
                    old is not None
                    and old["rev"] == it.rev
                    and (old["ts"] != it.ts or old["amplitude"] != it.amplitude)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "revision_conflict",
                            "sample_id": sid,
                            "rev": it.rev,
                        },
                    )

                to_upsert.append(it)
                if old is None:
                    content_changes.append((sid, None, it.ts))
                    recompute_needed = True
                else:
                    # Equal revision identical content = idempotent retry.
                    if old["ts"] != it.ts or old["amplitude"] != it.amplitude:
                        content_changes.append((sid, old["ts"], it.ts))
                        recompute_needed = True

            if to_upsert:
                _executemany(conn,
                    """
                    INSERT INTO readings
                        (device_id, sample_id, rev, ts, amplitude)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (device_id, sample_id) DO UPDATE
                       SET rev       = EXCLUDED.rev,
                           ts        = EXCLUDED.ts,
                           amplitude = EXCLUDED.amplitude,
                           updated_at = now()
                    """,
                    [
                        (device_id, it.sample_id, it.rev, it.ts, it.amplitude)
                        for it in to_upsert
                    ],
                )

            recomputed: dict[str, dict] = {}
            if recompute_needed:
                # Readings whose baseline window contains a changed reading at
                # its old or new position: r.ts in (t, t + 3600] for each t,
                # i.e. integer seconds [t+1, t+3600]. Changed rows themselves
                # are always included.
                intervals: list[tuple[int, int]] = []
                for _sid, old_ts, new_ts in content_changes:
                    intervals.append((new_ts + 1, new_ts + WINDOW_SECONDS))
                    if old_ts is not None and old_ts != new_ts:
                        intervals.append((old_ts + 1, old_ts + WINDOW_SECONDS))
                intervals = _merge_intervals(intervals)

                affected = {sid for sid, _o, _n in content_changes}
                for lo, hi in intervals:
                    for r in conn.execute(
                        """
                        SELECT sample_id FROM readings
                         WHERE device_id = %s AND ts BETWEEN %s AND %s
                        """,
                        (device_id, lo, hi),
                    ).fetchall():
                        affected.add(r["sample_id"])

                if affected:
                    recomputed = _recompute_baselines(
                        conn, device_id, affected
                    )

                _rebuild_alerts(conn, device_id, old_alerts)

            # Fill accepted responses (newly written or idempotent retry).
            for it in to_upsert:
                row = recomputed.get(it.sample_id) or existing.get(it.sample_id)
                results[it.sample_id] = {
                    "sample_id": it.sample_id,
                    "rev": it.rev,
                    "status": "accepted",
                    "ts": row["ts"],
                    "amplitude": float(row["amplitude"]),
                    "baseline": (
                        float(row["baseline"]) if row["baseline"] is not None else None
                    ),
                    "window_count": row["window_count"],
                    "hit": row["is_hit"],
                }

            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    # One result per request item, preserving request order. Within-batch
    # duplicates return their own verdict (ignored when a higher revision for
    # the same sample appears in the same batch).
    def _ignored(sample_id: str, rev: int) -> dict:
        return {
            "sample_id": sample_id,
            "rev": rev,
            "status": "ignored",
            "ts": None,
            "amplitude": None,
            "baseline": None,
            "window_count": 0,
            "hit": None,
        }

    winner_rev = {sid: it.rev for sid, it in wanted.items()}
    out: list[dict] = []
    for r in items:
        if r.rev < winner_rev[r.sample_id]:
            out.append(_ignored(r.sample_id, r.rev))
        else:
            out.append(results[r.sample_id])
    return out


def _median(sorted_vals: list[Decimal]) -> Decimal | None:
    """Exact median matching PostgreSQL percentile_cont(0.5): average of the
    two middle values for an even sample count."""
    n = len(sorted_vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def _recompute_baselines(conn, device_id: str, affected: set[str]) -> dict[str, dict]:
    """Recompute baseline / window_count / is_hit for the affected readings.

    One index range read pulls every reading whose timestamp could fall inside
    any affected window, medians are taken over a sliding window in Python
    (exact Decimal arithmetic, same-second strictly excluded), and one bulk
    UPDATE writes the results. This keeps a pure-append batch at O(window)
    instead of O(batch * window) correlated subqueries.
    """
    targets = conn.execute(
        """
        SELECT sample_id, ts, amplitude
          FROM readings
         WHERE device_id = %s AND sample_id = ANY(%s)
         ORDER BY ts, sample_id
        """,
        (device_id, list(affected)),
    ).fetchall()
    if not targets:
        return {}

    lo = targets[0]["ts"] - WINDOW_SECONDS
    hi = targets[-1]["ts"]
    candidates = conn.execute(
        """
        SELECT ts, sample_id, amplitude
          FROM readings
         WHERE device_id = %s AND ts BETWEEN %s AND %s
         ORDER BY ts, sample_id
        """,
        (device_id, lo, hi),
    ).fetchall()

    # Sliding window: for each target keep values with ts in
    # [target_ts - 3600, target_ts). Equal seconds never enter the window,
    # regardless of sample id.
    head = 0
    tail = 0
    params: list[tuple] = []
    out: dict[str, dict] = {}

    for tgt in targets:
        t = tgt["ts"]
        lower = t - WINDOW_SECONDS
        while head < len(candidates) and candidates[head]["ts"] < lower:
            head += 1
        while tail < len(candidates) and candidates[tail]["ts"] < t:
            tail += 1
        window = sorted(
            candidates[i]["amplitude"] for i in range(head, tail)
        )
        cnt = len(window)
        med = _median(window) if cnt >= MIN_BASELINE_SAMPLES else None
        is_hit = bool(med is not None and tgt["amplitude"] > 3 * med)
        params.append((med, cnt, is_hit, device_id, tgt["sample_id"]))
        out[tgt["sample_id"]] = {
            "sample_id": tgt["sample_id"],
            "ts": t,
            "amplitude": tgt["amplitude"],
            "baseline": med,
            "window_count": cnt,
            "is_hit": is_hit,
        }

    _executemany(
        conn,
        """
        UPDATE readings
           SET baseline = %s,
               window_count = %s,
               is_hit = %s,
               updated_at = now()
         WHERE device_id = %s AND sample_id = %s
        """,
        params,
    )
    return out


def _rebuild_alerts(conn, device_id: str, old_alerts: dict[str, tuple]) -> None:
    """Recompute merged alerts for one device and append outbox changes."""
    new_rows = conn.execute(
        """
        WITH seq AS (
            SELECT sample_id, ts, amplitude, is_hit,
                   lag(ts)      OVER w AS prev_ts,
                   lag(is_hit)  OVER w AS prev_hit
              FROM readings
             WHERE device_id = %(dev)s
            WINDOW w AS (ORDER BY ts, sample_id)
        ),
        marks AS (
            SELECT sample_id, ts, amplitude,
                   CASE WHEN is_hit
                         AND (prev_hit IS NULL OR NOT prev_hit
                              OR ts - prev_ts > %(gap)s)
                        THEN 1
                        WHEN is_hit THEN 0
                   END AS brk
              FROM seq
        ),
        grp AS (
            SELECT sample_id, ts, amplitude,
                   SUM(CASE WHEN brk = 1 THEN 1 ELSE 0 END)
                       OVER (ORDER BY ts, sample_id
                             ROWS UNBOUNDED PRECEDING) AS g
              FROM marks
             WHERE brk IS NOT NULL
        )
        SELECT g,
               (array_agg(sample_id ORDER BY ts, sample_id))[1]              AS alert_id,
               min(ts)                                                      AS start_ts,
               max(ts)                                                      AS end_ts,
               (array_agg(ts ORDER BY amplitude DESC, ts, sample_id))[1]    AS peak_ts,
               max(amplitude)                                               AS peak_amp,
               count(*)                                                     AS hit_count,
               array_agg(sample_id ORDER BY ts, sample_id)                  AS sample_ids
          FROM grp
         GROUP BY g
         ORDER BY g
        """,
        {"dev": device_id, "gap": MERGE_GAP_SECONDS},
    ).fetchall()

    new_alerts = {r["alert_id"]: (r, _alert_payload(r)) for r in new_rows}

    to_upsert = [r for r in new_rows]
    revoked = [aid for aid in old_alerts if aid not in new_alerts]

    if to_upsert:
        _executemany(conn,
            """
            INSERT INTO alerts
                (alert_id, device_id, start_ts, end_ts, peak_ts, peak_amp,
                 sample_ids, hit_count, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (device_id, alert_id) DO UPDATE
               SET start_ts   = EXCLUDED.start_ts,
                   end_ts     = EXCLUDED.end_ts,
                   peak_ts    = EXCLUDED.peak_ts,
                   peak_amp   = EXCLUDED.peak_amp,
                   sample_ids = EXCLUDED.sample_ids,
                   hit_count  = EXCLUDED.hit_count,
                   updated_at = now()
            """,
            [
                (
                    r["alert_id"],
                    device_id,
                    r["start_ts"],
                    r["end_ts"],
                    r["peak_ts"],
                    r["peak_amp"],
                    json.dumps(r["sample_ids"]),
                    r["hit_count"],
                )
                for r in to_upsert
            ],
        )

    if revoked:
        conn.execute(
            "DELETE FROM alerts WHERE device_id = %s AND alert_id = ANY(%s)",
            (device_id, revoked),
        )

    changes: list[tuple] = []
    for aid in revoked:
        changes.append((device_id, aid, "revoked", None))
    for aid, (row, payload) in new_alerts.items():
        old = old_alerts.get(aid)
        kind = "created" if old is None else ("updated" if old != payload else None)
        if kind:
            changes.append(
                (
                    device_id,
                    aid,
                    kind,
                    json.dumps(_alert_json(device_id, row)),
                )
            )

    if changes:
        _executemany(conn,
            """
            INSERT INTO alert_changes (device_id, alert_id, kind, alert)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            changes,
        )


def _alert_json(device_id: str, r: dict) -> dict:
    return {
        "alert_id": r["alert_id"],
        "device_id": device_id,
        "start_ts": r["start_ts"],
        "end_ts": r["end_ts"],
        "peak_ts": r["peak_ts"],
        "peak_amplitude": float(r["peak_amp"]),
        "hit_count": r["hit_count"],
        "sample_ids": list(r["sample_ids"]),
    }


def list_alerts(device_id: str | None, limit: int) -> list[dict]:
    pool = get_pool()
    with pool.connection() as conn:
        conn.row_factory = dict_row
        if device_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM alerts WHERE device_id = %s
                 ORDER BY start_ts, alert_id LIMIT %s
                """,
                (device_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM alerts
                 ORDER BY device_id, start_ts, alert_id LIMIT %s
                """,
                (limit,),
            ).fetchall()
    return [_alert_json(r["device_id"], r) for r in rows]


def list_changes(cursor: int, limit: int) -> tuple[list[dict], int | None]:
    pool = get_pool()
    with pool.connection() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            """
            SELECT seq, device_id, alert_id, kind, alert, changed_at
              FROM alert_changes
             WHERE seq > %s
             ORDER BY seq
             LIMIT %s
            """,
            (cursor, limit),
        ).fetchall()

    changes = []
    for r in rows:
        alert = r["alert"]
        changes.append(
            {
                "seq": r["seq"],
                "device_id": r["device_id"],
                "alert_id": r["alert_id"],
                "kind": r["kind"],
                "alert": alert,
                "changed_at": r["changed_at"].isoformat(),
            }
        )
    next_cursor = rows[-1]["seq"] if len(rows) == limit else None
    return changes, next_cursor
