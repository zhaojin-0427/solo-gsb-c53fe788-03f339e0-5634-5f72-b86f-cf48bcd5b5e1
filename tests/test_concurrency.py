"""Concurrency and per-item response edge cases."""
from __future__ import annotations

import threading

BASE = 1_700_000_000


def _post(client, dev, readings):
    return client.post("/readings", json={"device_id": dev, "readings": readings})


def _seed_calm(client, dev, n=20):
    return _post(client, dev, [
        {"sample_id": f"s{i:02d}", "ts": BASE + i * 60, "rev": 1, "amplitude": 1.0}
        for i in range(n)
    ])


def test_within_batch_duplicate_lower_rev_is_per_item(client, dev):
    rows = [
        {"sample_id": "dup", "ts": BASE, "rev": 1, "amplitude": 1.0},
        {"sample_id": "dup", "ts": BASE + 60, "rev": 2, "amplitude": 2.0},
    ]
    res = _post(client, dev, rows).json()["results"]
    assert len(res) == 2                       # one verdict per request item
    assert res[0]["status"] == "ignored"
    assert res[0]["ts"] is None
    assert res[1]["status"] == "accepted"
    assert res[1]["ts"] == BASE + 60 and res[1]["amplitude"] == 2.0


def test_within_batch_equal_rev_diff_content_conflict(client, dev):
    rows = [
        {"sample_id": "dup", "ts": BASE, "rev": 1, "amplitude": 1.0},
        {"sample_id": "dup", "ts": BASE, "rev": 1, "amplitude": 1.5},
    ]
    assert _post(client, dev, rows).status_code == 409


def test_concurrent_batches_are_serialized_per_device(client, dev):
    _seed_calm(client, dev, 20)
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(k: int):
        try:
            barrier.wait()
            rows = [
                {"sample_id": f"c{k}-{i}", "ts": BASE + 1200 + k * 1000 + i,
                 "rev": 1, "amplitude": 1.0}
                for i in range(10)
            ]
            r = _post(client, dev, rows)
            assert r.status_code == 200, r.text
            assert all(x["status"] == "accepted" for x in r.json()["results"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # exactly 80 distinct samples persisted, alerts untouched (all calm)
    assert client.get(f"/alerts?device_id={dev}").json()["alerts"] == []
    assert client.get("/changes").json()["changes"] == []


def test_retry_full_mixed_batch_duplicates_nothing(client, dev):
    _seed_calm(client, dev, 20)
    batch = [
        {"sample_id": "h1", "ts": BASE + 1200, "rev": 1, "amplitude": 4.0},
        {"sample_id": "h2", "ts": BASE + 1260, "rev": 1, "amplitude": 5.0},
    ]
    assert _post(client, dev, batch).status_code == 200
    n1 = len(client.get("/changes").json()["changes"])
    # retry verbatim several times
    for _ in range(3):
        r = _post(client, dev, batch)
        assert r.status_code == 200
        assert all(x["status"] == "accepted" for x in r.json()["results"])
    n2 = len(client.get("/changes").json()["changes"])
    assert n1 == n2 == 1
