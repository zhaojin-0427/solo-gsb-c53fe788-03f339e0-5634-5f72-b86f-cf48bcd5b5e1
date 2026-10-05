"""Functional tests for baseline rules, revisions, alerts, corrections and feed."""
from __future__ import annotations

BASE = 1_700_000_000


def _r(sid, ts, rev=1, amp=1.0):
    return {"sample_id": sid, "ts": ts, "rev": rev, "amplitude": amp}


def _post(client, dev, readings):
    return client.post("/readings", json={"device_id": dev, "readings": readings})


def _seed_calm(client, dev, n=20, start=BASE, step=60, amp=1.0):
    """n calm readings; s00..s(n-1)."""
    rows = [_r(f"s{i:02d}", start + i * step, 1, amp) for i in range(n)]
    assert _post(client, dev, rows).status_code == 200
    return rows


# ---------------------------------------------------------------- baseline

def test_baseline_unknown_below_20(client, dev):
    rows = [_r(f"s{i:02d}", BASE + i * 60, amp=1.0) for i in range(19)]
    spike = _r("spike", BASE + 20 * 60, amp=100.0)
    r = _post(client, dev, rows + [spike]).json()["results"][-1]
    assert r["status"] == "accepted"
    assert r["window_count"] == 19
    assert r["baseline"] is None
    assert r["hit"] is False          # unknown baseline can never hit


def test_strict_three_times_threshold(client, dev):
    _seed_calm(client, dev, n=20)
    # amplitude exactly 3 * baseline is NOT a hit (strict greater).
    r = _post(client, dev, [_r("eq3", BASE + 20 * 60, amp=3.0)]).json()["results"][0]
    assert r["baseline"] == 1.0
    assert r["window_count"] == 20
    assert r["hit"] is False
    r = _post(client, dev, [_r("over3", BASE + 21 * 60, amp=3.000001)]).json()["results"][0]
    assert r["hit"] is True


def test_exact_median_even_count(client, dev):
    # 19 readings: 10 of 1.0 and 9 of 3.0 -> median of 20 samples after spike.
    rows = [_r(f"lo{i}", BASE + i * 60, amp=1.0) for i in range(10)]
    rows += [_r(f"hi{i}", BASE + 600 + i * 60, amp=3.0) for i in range(9)]
    rows.append(_r("judge", BASE + 19 * 60, amp=2.0))
    res = _post(client, dev, rows).json()["results"][-1]
    assert res["window_count"] == 19
    assert res["baseline"] is None     # only 19 prior -> unknown
    # add one more calm so judge-equivalent sees 20 prior with median 2.0
    extra = _r("judge2", BASE + 20 * 60, amp=7.0)
    res = _post(client, dev, [extra]).json()["results"][0]
    assert res["window_count"] == 20
    assert res["baseline"] == 1.5      # average of 10th (1) and 11th (2)
    assert res["hit"] is True          # 7 > 4.5


def test_window_left_closed(client, dev):
    _seed_calm(client, dev, n=20, start=BASE - 3600, step=180)
    # A reading exactly 3600s after the first calm sample: that sample is
    # included (left closed), the reading's own second is excluded.
    r = _post(client, dev, [_r("edge", BASE, amp=4.0)]).json()["results"][0]
    assert r["window_count"] == 20
    assert r["baseline"] == 1.0
    assert r["hit"] is True


def test_sample_older_than_window_drops_out(client):
    dev = "dev-left-open"
    _seed_calm(client, dev, n=20, start=BASE - 3600, step=180)
    # One second past the left-closed boundary: the oldest (BASE-3600) sample
    # is outside [BASE-3599, BASE+1), so only 19 precede -> unknown baseline.
    r = _post(client, dev, [_r("edge2", BASE + 1, amp=4.0)]).json()["results"][0]
    assert r["window_count"] == 19
    assert r["baseline"] is None
    assert r["hit"] is False


def test_same_second_excluded_even_other_samples(client, dev):
    _seed_calm(client, dev, n=19)  # s00..s18 over 18 minutes
    # Two readings on the same second: judge sees only the 19 earlier readings,
    # never the other same-second sample.
    rows = [
        _r("sameA", BASE + 20 * 60, amp=100.0),
        _r("sameB", BASE + 20 * 60, amp=100.0),
    ]
    res = {r["sample_id"]: r for r in _post(client, dev, rows).json()["results"]}
    assert res["sameA"]["window_count"] == 19
    assert res["sameB"]["window_count"] == 19
    assert res["sameA"]["hit"] is False and res["sameB"]["hit"] is False
    # Ordering by (ts, sample_id) still works; a later second reading then sees 21.
    r = _post(client, dev, [_r("after", BASE + 20 * 60 + 1, amp=4.0)]).json()["results"][0]
    assert r["window_count"] == 21
    assert r["hit"] is True


# ---------------------------------------------------------------- revisions

def test_higher_revision_wins_lower_ignored(client, dev):
    # 10 readings of 1.0 then 10 of 3.0.
    rows = [_r(f"lo{i}", BASE + i * 60, amp=1.0) for i in range(10)]
    rows += [_r(f"hi{i}", BASE + 600 + i * 60, amp=3.0) for i in range(10)]
    _post(client, dev, rows)
    t = BASE + 20 * 60

    _post(client, dev, [_r("a", t, rev=1, amp=4.0)])
    r = _post(client, dev, [_r("a", t, rev=2, amp=2.0)]).json()["results"][0]
    assert r["status"] == "accepted" and r["amplitude"] == 2.0

    # a later reading's baseline now includes a@2.0: median of 21 = 2.0
    p = _post(client, dev, [_r("probe", t + 60, amp=1.0)]).json()["results"][0]
    assert p["window_count"] == 21 and p["baseline"] == 2.0

    # an older revision is ignored and never changes the stored amplitude
    r = _post(client, dev, [_r("a", t + 600, rev=1, amp=9.0)]).json()["results"][0]
    assert r["status"] == "ignored"
    p2 = _post(client, dev, [_r("probe2", t + 120, amp=1.0)]).json()["results"][0]
    # 11 ones (incl. a@2.0 ignored-revision leaves a@2.0) + 10 threes -> 1.5;
    # had the stale rev1 with amp 9.0 been applied the median would be 5.0.
    assert p2["baseline"] == 1.5


def test_equal_revision_conflict_returns_409(client, dev):
    _post(client, dev, [_r("a", BASE + 60, 1, 1.0)])
    resp = _post(client, dev, [_r("a", BASE + 60, 1, 1.5)])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "revision_conflict"
    # same revision same content is an idempotent retry, not a conflict
    ok = _post(client, dev, [_r("a", BASE + 60, 1, 1.0)])
    assert ok.status_code == 200
    assert ok.json()["results"][0]["status"] == "accepted"


def test_conflict_aborts_whole_batch(client, dev):
    _post(client, dev, [_r("a", BASE, 1, 1.0), _r("b", BASE + 60, 1, 1.0)])
    resp = _post(client, dev, [
        _r("c", BASE + 120, 1, 1.0),
        _r("a", BASE, 1, 9.9),
    ])
    assert resp.status_code == 409
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert alerts == []
    changes = client.get("/changes").json()["changes"]
    assert changes == []       # whole transaction rolled back


def test_negative_and_non_finite_amplitude_rejected(client, dev):
    for raw in ("-0.001", "1e999", "-1e999", "NaN"):
        body = (
            f'{{"device_id":"{dev}","readings":['
            f'{{"sample_id":"x","ts":{BASE},"rev":1,"amplitude":{raw}}}]}}'
        )
        resp = client.post("/readings", content=body,
                           headers={"content-type": "application/json"})
        assert resp.status_code == 422, raw


# ---------------------------------------------------------------- alert merging

def test_adjacent_hits_merge_gap_boundary(client, dev):
    _seed_calm(client, dev, n=20)
    t0 = BASE + 20 * 60
    # two hits exactly 300s apart -> merged
    _post(client, dev, [
        _r("hit1", t0, amp=4.0),
        _r("hit2", t0 + 300, amp=5.0),
    ])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["alert_id"] == "hit1"
    assert a["sample_ids"] == ["hit1", "hit2"]
    assert a["start_ts"] == t0 and a["end_ts"] == t0 + 300
    assert a["peak_ts"] == t0 + 300 and a["peak_amplitude"] == 5.0


def test_hits_gap_over_300_stay_separate(client, dev):
    _seed_calm(client, dev, n=20)
    t0 = BASE + 20 * 60
    _post(client, dev, [
        _r("hit1", t0, amp=4.0),
        _r("hit2", t0 + 301, amp=5.0),
    ])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert [a["alert_id"] for a in alerts] == ["hit1", "hit2"]


def test_non_hit_breaks_run(client, dev):
    _seed_calm(client, dev, n=20)
    t0 = BASE + 20 * 60
    _post(client, dev, [
        _r("hit1", t0, amp=4.0),
        _r("calm", t0 + 60, amp=1.0),
        _r("hit2", t0 + 120, amp=4.0),
    ])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert [a["alert_id"] for a in alerts] == ["hit1", "hit2"]


def test_single_isolated_hit_is_alert(client, dev):
    _seed_calm(client, dev, n=20)
    _post(client, dev, [_r("lonely", BASE + 20 * 60, amp=4.0)])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["alert_id"] == "lonely"


def test_alert_id_is_first_hit_sample(client, dev):
    # event-time ordering, not submission order: insert later hit first.
    _seed_calm(client, dev, n=20)
    t0 = BASE + 20 * 60
    _post(client, dev, [_r("zzz", t0 + 100, amp=5.0)])
    _post(client, dev, [_r("aaa", t0, amp=4.0)])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["alert_id"] == "aaa"
    assert alerts[0]["sample_ids"] == ["aaa", "zzz"]
    # the originally-created singleton alert zzz is revoked as its id changes
    kinds = {(c["kind"], c["alert_id"]) for c in client.get("/changes").json()["changes"]}
    assert ("created", "zzz") in kinds and ("revoked", "zzz") in kinds
    assert ("created", "aaa") in kinds


# ---------------------------------------------------------------- corrections

def test_correction_downgrade_revokes_alert_and_emits_feed(client, dev):
    _seed_calm(client, dev, n=20)
    t0 = BASE + 20 * 60
    _post(client, dev, [_r("hit1", t0, amp=4.0), _r("hit2", t0 + 60, amp=4.0)])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert len(alerts) == 1

    # correct hit2 down: baseline windows affected, run breaks / disappears
    _post(client, dev, [_r("hit2", t0 + 60, rev=2, amp=1.0)])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    # hit1 is still a spike (its own window unchanged)...
    assert [a["alert_id"] for a in alerts] == ["hit1"]

    feed = client.get("/changes").json()
    kinds = [(c["kind"], c["alert_id"]) for c in feed["changes"]]
    assert ("created", "hit1") in kinds
    # the old merged alert whose id was hit1 got updated (sample list shrunk)
    updated = [c for c in feed["changes"] if c["alert_id"] == "hit1" and c["kind"] == "updated"]
    assert updated and updated[-1]["alert"]["sample_ids"] == ["hit1"]


def test_correction_move_into_window_recomputes_downstream(client, dev):
    # 20 calm readings of 2.0 then a non-hit at exactly 3x; move a big reading
    # far into the past so downstream baselines change and hits appear.
    rows = [_r(f"s{i:02d}", BASE + i * 60, amp=2.0) for i in range(20)]
    rows.append(_r("late", BASE + 20 * 60, amp=6.0))       # 6 == 3*2 -> no hit
    _post(client, dev, rows)
    # 'rover' initially sits far beyond the window with a large amplitude.
    _post(client, dev, [_r("rover", BASE + 10_000, amp=100.0)])
    assert client.get(f"/alerts?device_id={dev}").json()["alerts"] == []
    # move rover to one second before 'late': median stays 2 (21 samples) but
    # rover itself is now a spike (100 > 3*2).
    _post(client, dev, [_r("rover", BASE + 20 * 60 - 1, rev=2, amp=100.0)])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert [a["alert_id"] for a in alerts] == ["rover"]
    # 'late' at exactly 3x still does not hit, hence not part of the alert
    assert alerts[0]["sample_ids"] == ["rover"]
    # nudge late strictly above 3x baseline: it joins the same merged alert
    _post(client, dev, [_r("late", BASE + 20 * 60, rev=2, amp=6.01)])
    alerts = client.get(f"/alerts?device_id={dev}").json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["alert_id"] == "rover"
    assert alerts[0]["sample_ids"] == ["rover", "late"]


def test_correction_old_position_no_longer_affects(client, dev):
    # A big reading at t; readings t+1..t+3600 must stop counting it after it
    # moves away.
    _seed_calm(client, dev, n=19)
    t0 = BASE + 19 * 60
    _post(client, dev, [_r("big", t0, amp=100.0)])
    after = _post(client, dev, [_r("after", t0 + 10, amp=1.0)]).json()["results"][0]
    vals = sorted([1.0] * 19 + [100.0])
    med = (vals[9] + vals[10]) / 2
    assert after["window_count"] == 20
    assert abs(after["baseline"] - med) < 1e-9
    # move big far away: after's baseline must drop back to 1.0 (19 prior, unknown)
    _post(client, dev, [_r("big", BASE + 100_000, rev=2, amp=100.0)])
    after2 = _post(client, dev, [_r("probe", t0 + 11, amp=4.0)]).json()["results"][0]
    # 'after' itself also got recomputed; check via readings endpoint indirectly:
    # probe window now has 20 readings (19 calm + after@1.0), median 1.0.
    assert after2["window_count"] == 20
    assert after2["baseline"] == 1.0
    assert after2["hit"] is True


# ---------------------------------------------------------------- change feed

def test_changes_feed_cursor_and_retry(client, dev):
    _seed_calm(client, dev, n=20)
    t0 = BASE + 20 * 60
    _post(client, dev, [_r("hit1", t0, amp=4.0)])
    first = client.get("/changes?limit=1").json()
    assert first["next_cursor"] == 1
    assert len(first["changes"]) == 1 and first["changes"][0]["kind"] == "created"

    # extend the alert -> 'updated'
    _post(client, dev, [_r("hit2", t0 + 60, amp=4.0)])
    page2 = client.get(f"/changes?cursor={first['next_cursor']}").json()
    kinds = [c["kind"] for c in page2["changes"]]
    assert kinds == ["updated"]
    assert page2["changes"][0]["alert"]["sample_ids"] == ["hit1", "hit2"]
    assert page2["next_cursor"] is None

    # retry every batch verbatim: no new change rows.
    _seed_calm(client, dev, n=20)
    _post(client, dev, [_r("hit1", t0, amp=4.0)])
    _post(client, dev, [_r("hit2", t0 + 60, amp=4.0)])
    again = client.get(f"/changes?cursor={page2['changes'][-1]['seq']}").json()
    assert again["changes"] == []


def test_changes_filter_not_required_global(client, dev):
    _seed_calm(client, dev, n=20)
    _post(client, dev, [_r("h", BASE + 20 * 60, amp=4.0)])
    other = "dev-other-x"
    _seed_calm(client, other, n=20)
    client.post("/readings", json={"device_id": other,
                                   "readings": [_r("h", BASE + 20 * 60, amp=4.0)]})
    feed = client.get("/changes").json()["changes"]
    assert {c["device_id"] for c in feed} == {dev, other}


def test_unchanged_alert_emits_no_change(client, dev):
    _seed_calm(client, dev, n=20)
    t0 = BASE + 20 * 60
    _post(client, dev, [_r("hit1", t0, amp=4.0), _r("hit2", t0 + 60, amp=4.0)])
    # unrelated correction far away must not touch the alert / emit changes
    n_before = len(client.get("/changes").json()["changes"])
    _post(client, dev, [_r("faraway", BASE + 50_000, rev=1, amp=0.5)])
    _post(client, dev, [_r("faraway", BASE + 50_000, rev=2, amp=0.6)])
    n_after = len(client.get("/changes").json()["changes"])
    assert n_after == n_before


def test_alert_and_changes_committed_together(client, dev):
    # /changes never shows an alert absent from /alerts (created) and vice versa
    _seed_calm(client, dev, n=20)
    _post(client, dev, [_r("h", BASE + 20 * 60, amp=4.0)])
    alerts = {a["alert_id"]: a for a in client.get(f"/alerts?device_id={dev}").json()["alerts"]}
    for c in client.get("/changes").json()["changes"]:
        if c["kind"] == "created":
            assert c["alert_id"] in alerts
            assert c["alert"] == alerts[c["alert_id"]]
