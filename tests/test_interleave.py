"""Interleaving / consistency tests.

Covers the rule: while imports, certificate edits, publishes and queries are
interleaved, each published version must derive from a single committed state
of readings and certificates; failed writes change nothing; stale old
versions stay queryable until a successful recompute replaces them.
"""
import json
import threading

import pytest

from app import db, services
from app.timeutil import day_to_iso, parse_day

from conftest import CSV_HEADER

DAY = "2024-01-01"
BASE = parse_day(DAY) * 1440
DAY_IDX = BASE // 1440


def _history_rows():
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT id, probe_id, day, data_version, status, payload "
            "FROM publications ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _latest(probe="P-01"):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT * FROM publications WHERE probe_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (probe,),
        ).fetchone()
    finally:
        conn.close()


def _snapshot_version() -> int:
    conn = db.connect()
    try:
        return db.current_version(conn)
    finally:
        conn.close()


def test_interleaved_steps_single_version_consistency(service_db):
    # 1. readings + certificate, first publish at version 2
    services.import_readings(
        CSV_HEADER
        + f"P-01,{BASE},8.5\nP-01,{BASE + 1},9.0\nP-01,{BASE + 2},7.0\n"
    )
    services.add_certificate("P-01", BASE, BASE + 3, 0.0)
    p1 = services.publish_day("P-01", DAY_IDX)
    assert p1["data_version"] == 2
    assert [(s["start_minute"], s["end_minute"]) for s in
            p1["payload"]["segments"]] == [(BASE, BASE + 2)]

    # 2. failed import (bad temperature) changes neither data nor stale state
    with pytest.raises(services.ServiceError):
        services.import_readings(CSV_HEADER + f"P-01,{BASE},nope\n")
    latest = _latest()
    assert latest["status"] == "serving"

    # 3. a second certificate touching the same UTC day itself stales the day
    services.add_certificate("P-01", BASE + 4, BASE + 6, 0.0)
    assert _latest()["status"] == "stale"
    # failed overlapping edit -> 409, state (including the stale flag) unchanged
    with pytest.raises(services.ServiceError) as ei:
        services.update_certificate(2, valid_from=BASE + 1, valid_to=BASE + 5)
    assert ei.value.status_code == 409
    assert _latest()["status"] == "stale"
    assert len(_history_rows()) == 1

    # 4. successful correction keeps the published day stale (already stale)
    services.import_readings(CSV_HEADER + f"P-01,{BASE + 1},6.0\n")
    latest = _latest()
    assert latest["status"] == "stale"
    # stale old version still queryable with its original payload
    assert latest["payload"] is not None
    import json
    old_payload = json.loads(latest["payload"])
    assert old_payload["segments"][0]["start_minute"] == BASE

    # 5. failed recompute does not replace the stale version
    def boom(*_a, **_kw):
        raise services.ComputeError("injected")

    original = services.compute_day
    services.compute_day = boom
    try:
        with pytest.raises(services.ServiceError):
            services.publish_day("P-01", DAY_IDX)
    finally:
        services.compute_day = original
    assert len(_history_rows()) == 1
    assert _latest()["status"] == "stale"

    # 6. successful recompute replaces the serving version atomically
    p2 = services.publish_day("P-01", DAY_IDX)
    assert p2["status"] == "serving"
    assert p2["data_version"] == _snapshot_version()
    rows = _history_rows()
    assert len(rows) == 2
    assert [r["status"] for r in rows] == ["superseded", "serving"]
    # new judgment: BASE+1 corrected below threshold; BASE itself stays hot
    assert [(s["start_minute"], s["end_minute"]) for s in
            p2["payload"]["segments"]] == [(BASE, BASE + 1)]


def test_every_publication_payload_matches_one_committed_snapshot(service_db):
    """Each publication must equal a recomputation over one committed state.

    UPSERT overwrites rows in place, so an old snapshot cannot be rebuilt via
    data_version filters; instead the test maintains a mirror of the committed
    state, applies every *successful* mutation to both sides, and recomputes
    the expected payload at each publish from the mirror.
    """
    from app.engine import Calibration, Reading, compute_day

    mirror_readings: dict[int, float] = {}
    mirror_cals: dict[int, list] = {}  # cert id -> [from, to, offset]

    def expect_payload():
        payload = compute_day(
            "P-01",
            DAY_IDX,
            [Reading(m, t) for m, t in sorted(mirror_readings.items())],
            [Calibration(*c) for c in sorted(mirror_cals.values())],
        )
        stored = json.loads(_latest()["payload"])
        assert stored["segments"] == payload["segments"]
        assert stored["uncalibrated_minutes"] == payload["uncalibrated_minutes"]
        assert stored["calibrated_minutes"] == payload["calibrated_minutes"]
        assert stored["threshold_c"] == 8.0
        return stored

    # first committed state -> first publication
    for m, t in ((BASE, 8.5), (BASE + 1, 9.2), (BASE + 2, 9.3)):
        mirror_readings[m] = t
    services.import_readings(
        CSV_HEADER
        + f"P-01,{BASE},8.5\nP-01,{BASE + 1},9.2\nP-01,{BASE + 2},9.3\n"
    )
    mirror_cals[1] = [BASE, BASE + 3, 0.1]
    services.add_certificate("P-01", BASE, BASE + 3, 0.1)
    pub1 = services.publish_day("P-01", DAY_IDX)
    assert pub1["data_version"] == _snapshot_version()
    expect_payload()

    # correction + certificate edit: the next publication must reflect BOTH,
    # i.e. one single newer committed state rather than a mix.
    mirror_readings[BASE + 1] = 7.0
    services.import_readings(CSV_HEADER + f"P-01,{BASE + 1},7.0\n")
    mirror_cals[1] = [BASE, BASE + 3, 0.4]
    services.update_certificate(1, offset_c=0.4)
    pub2 = services.publish_day("P-01", DAY_IDX)
    assert pub2["data_version"] == _snapshot_version()
    stored2 = expect_payload()
    # concrete check of the combined judgment with offset 0.4:
    # cal values: 8.9 / 7.4 / 9.7 -> hot at BASE and BASE+2, separated by the
    # below-threshold minute BASE+1, so two truncated segments.
    assert [(s["start_minute"], s["end_minute"]) for s in stored2["segments"]] == [
        (BASE, BASE + 1),
        (BASE + 2, BASE + 3),
    ]

    # failed mutation changes neither side; a republish still matches the mirror
    with pytest.raises(services.ServiceError):
        services.import_readings(CSV_HEADER + f"P-01,{BASE},bad\n")
    services.publish_day("P-01", DAY_IDX)
    expect_payload()


def test_concurrent_writers_are_serialized_and_always_consistent(service_db):
    services.import_readings(
        CSV_HEADER
        + f"P-01,{BASE},8.5\nP-01,{BASE + 1},9.0\nP-01,{BASE + 2},9.1\n"
    )
    services.add_certificate("P-01", BASE, BASE + 3, 0.0)
    services.publish_day("P-01", DAY_IDX)

    errors = []

    def correction_worker():
        try:
            for i in range(15):
                services.import_readings(
                    CSV_HEADER
                    + f"P-01,{BASE + 1},{8.0 + (i % 3) * 0.4}\n"
                )
        except Exception as e:  # pragma: no cover - failure reporter
            errors.append(e)

    def cert_worker():
        try:
            for i in range(10):
                # offset edits on the same certificate; each is a full tx
                services.update_certificate(1, offset_c=(i % 5) * 0.1)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def publish_worker():
        try:
            for _ in range(12):
                services.publish_day("P-01", DAY_IDX)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def bad_import_worker():
        try:
            for _ in range(10):
                try:
                    services.import_readings(CSV_HEADER + f"P-01,{BASE},bad\n")
                except services.ServiceError:
                    pass
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [
        threading.Thread(target=correction_worker),
        threading.Thread(target=cert_worker),
        threading.Thread(target=publish_worker),
        threading.Thread(target=bad_import_worker),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []

    # Invariants after the storm:
    conn = db.connect()
    try:
        current = conn.execute(
            "SELECT * FROM publications WHERE status IN ('serving','stale')"
        ).fetchall()
        assert len(current) == 1  # exactly one current version per probe/day
        row = current[0]
        global_version = db.current_version(conn)
        if row["status"] == "serving":
            # a serving row must reflect the latest committed snapshot
            assert row["data_version"] == global_version
        else:
            # a stale row predates the change that invalidated it
            assert row["data_version"] < global_version
        # readings table: one row per probe/minute, finite values
        n = conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        assert n == 3
        # certificates remain non-overlapping
        certs = conn.execute(
            "SELECT valid_from, valid_to FROM calibrations WHERE probe_id = 'P-01' "
            "ORDER BY valid_from"
        ).fetchall()
        for a, b in zip(certs, certs[1:]):
            assert b["valid_from"] >= a["valid_to"]
    finally:
        conn.close()

    # final recompute must always succeed on the committed state
    final = services.publish_day("P-01", DAY_IDX)
    assert final["status"] == "serving"
    assert final["data_version"] == _snapshot_version()
    assert day_to_iso(DAY_IDX) == DAY
