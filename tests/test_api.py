"""Functional tests: import/correction, certificates, publishing, staleness."""
from app import services
from app.engine import Calibration, Reading, compute_day
from app.timeutil import parse_day

from conftest import CSV_HEADER, add_cert, import_csv

DAY = "2024-01-01"
BASE = parse_day(DAY) * 1440  # 2024-01-01T00:00Z epoch minutes
assert BASE == 28401120


# --------------------------------------------------------------------------- #
# Engine (pure function)
# --------------------------------------------------------------------------- #
def test_engine_segments_break_on_missing_and_uncalibrated():
    # minutes: 0,1,2,3 hot; 4 missing; 5,6 hot; 7 uncalibrated; 8 hot
    readings = [
        Reading(BASE + 0, 8.5),
        Reading(BASE + 1, 9.0),
        Reading(BASE + 2, 9.2),
        Reading(BASE + 3, 8.1),
        Reading(BASE + 5, 8.4),
        Reading(BASE + 6, 8.6),
        Reading(BASE + 7, 9.9),
        Reading(BASE + 8, 9.1),
    ]
    cals = [Calibration(BASE, BASE + 7, 0.0), Calibration(BASE + 8, BASE + 9, 0.0)]
    payload = compute_day("P", BASE // 1440, readings, cals)

    assert payload["uncalibrated_minutes"] == [BASE + 7]
    assert len(payload["segments"]) == 3
    assert payload["segments"][0]["start_minute"] == BASE
    assert payload["segments"][0]["end_minute"] == BASE + 4  # half-open
    assert payload["segments"][0]["duration_minutes"] == 4
    assert payload["segments"][1]["start_minute"] == BASE + 5
    assert payload["segments"][1]["end_minute"] == BASE + 7
    assert payload["segments"][2]["start_minute"] == BASE + 8
    assert payload["segments"][2]["end_minute"] == BASE + 9
    # peak tracking
    assert payload["segments"][0]["max_calibrated_c"] == 9.2


def test_engine_offset_applied_and_threshold_is_strict():
    # 7.9 + 0.2 = 8.1 -> above; 7.7 + 0.2 = 7.9 -> below
    readings = [Reading(BASE, 7.9), Reading(BASE + 1, 7.7)]
    payload = compute_day(
        "P", BASE // 1440, readings, [Calibration(BASE, BASE + 2, 0.2)]
    )
    assert len(payload["segments"]) == 1
    assert payload["segments"][0]["start_minute"] == BASE
    # only minute BASE qualifies; half-open end is BASE + 1
    assert payload["segments"][0]["end_minute"] == BASE + 1


def test_engine_threshold_8_itself_is_not_above():
    # calibrated value exactly 8.0 must not open a segment (strictly > 8 C)
    readings = [Reading(BASE, 8.0), Reading(BASE + 1, 8.001)]
    payload = compute_day(
        "P", BASE // 1440, readings, [Calibration(BASE, BASE + 2, 0.0)]
    )
    assert len(payload["segments"]) == 1
    assert payload["segments"][0]["start_minute"] == BASE + 1


def test_engine_half_open_certificate_boundary():
    # cert [BASE+1, BASE+2): minute BASE+1 covered, BASE+2 is a gap
    readings = [Reading(BASE + 1, 9.0), Reading(BASE + 2, 9.0)]
    payload = compute_day(
        "P", BASE // 1440, readings, [Calibration(BASE + 1, BASE + 2, 0.0)]
    )
    assert payload["uncalibrated_minutes"] == [BASE + 2]
    assert payload["segments"][0]["start_minute"] == BASE + 1


# --------------------------------------------------------------------------- #
# HTTP API: import & correction
# --------------------------------------------------------------------------- #
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["data_version"] == 0


def test_import_insert_then_correct_replaces(client):
    r = import_csv(
        client,
        CSV_HEADER + f"P-01,{BASE},7.5\nP-01,{BASE + 1},9.0\n",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 2 and body["replaced"] == 0
    assert body["data_version"] == 1

    # correction: same probe/minute, new temperature; within-file dup also last-wins
    r2 = import_csv(
        client,
        CSV_HEADER
        + f"P-01,{BASE},6.0\nP-01,{BASE},6.5\nP-02,{BASE},5.0\n",
    )
    body2 = r2.json()
    assert body2["rows_applied"] == 2  # duplicate within file collapsed
    assert body2["replaced"] == 1 and body2["inserted"] == 1
    assert body2["data_version"] == 2

    rows = client.get("/api/readings?probe_id=P-01&day=2024-01-01").json()["readings"]
    by_min = {x["minute_utc"]: x["temperature_c"] for x in rows}
    assert by_min[BASE] == 6.5  # corrected value, not 7.5 / 6.0
    assert by_min[BASE + 1] == 9.0


def test_import_accepts_iso_utc_timestamps(client):
    r = import_csv(
        client,
        "probe_id,minute_utc,temperature_c\n"
        "P-01,2024-01-01T00:00Z,8.5\n"
        "P-01,2024-01-01T00:01+00:00,9.5\n",
    )
    assert r.status_code == 200
    rows = client.get("/api/readings?probe_id=P-01").json()["readings"]
    assert [x["minute_utc"] for x in rows] == [BASE, BASE + 1]


def test_import_bad_csv_leaves_state_untouched(client):
    import_csv(client, CSV_HEADER + f"P-01,{BASE},7.5\n")
    v0 = client.get("/health").json()["data_version"]

    for bad in (
        "probe_id,minute_utc,temperature_c\nP-01,not-a-time,8\n",
        "probe_id,minute_utc\nP-01,28800\n",
        f"probe_id,minute_utc,temperature_c\nP-01,{BASE},notnum\n",
        "\n",
    ):
        r = import_csv(client, bad)
        assert r.status_code == 400, r.text
    assert client.get("/health").json()["data_version"] == v0
    rows = client.get("/api/readings?probe_id=P-01").json()["readings"]
    assert len(rows) == 1 and rows[0]["temperature_c"] == 7.5


def test_naive_timestamp_rejected(client):
    r = import_csv(
        client, "probe_id,minute_utc,temperature_c\nP-01,2024-01-01T00:00,8\n"
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Certificates
# --------------------------------------------------------------------------- #
def test_certificate_overlap_rejected_and_atomic(client):
    assert add_cert(client, "P-01", 100, 200, 0.1).status_code == 200
    # touching intervals are fine
    assert add_cert(client, "P-01", 200, 300, 0.2).status_code == 200
    # overlapping is rejected with 409
    r = add_cert(client, "P-01", 150, 250, 0.3)
    assert r.status_code == 409

    certs = client.get("/api/certificates?probe_id=P-01").json()["certificates"]
    assert len(certs) == 2  # failed insert did not land


def test_certificate_update_delete_and_404(client):
    cid = add_cert(client, "P-01", 100, 200, 0.1).json()["certificate_id"]
    r = client.patch(
        f"/api/certificates/{cid}", json={"valid_from": 50, "valid_to": 99}
    )
    assert r.status_code == 200 and r.json()["status"] == "committed"

    assert client.patch("/api/certificates/999", json={"offset_c": 1.0}).status_code == 404
    assert client.delete("/api/certificates/999").status_code == 404
    assert client.delete(f"/api/certificates/{cid}").status_code == 200
    assert client.get("/api/certificates").json()["count"] == 0


def test_invalid_half_open_interval_rejected(client):
    r = add_cert(client, "P-01", 200, 200, 0.1)
    assert r.status_code == 400
    r = add_cert(client, "P-01", 300, 200, 0.1)
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Publishing & staleness lifecycle
# --------------------------------------------------------------------------- #
def _seed_day(client):
    import_csv(
        client,
        CSV_HEADER
        + f"P-01,{BASE},7.4\n"          # cal 7.5 (below)
          f"P-01,{BASE + 1},8.3\n"      # cal 8.4 above
          f"P-01,{BASE + 2},9.1\n"      # cal 9.2 above
          f"P-01,{BASE + 3},9.4\n"      # cal 9.5 above
          f"P-01,{BASE + 5},8.8\n"      # minute BASE+4 missing -> break
          f"P-01,{BASE + 6},7.9\n",     # cal 8.0 -> not strictly above
    )
    add_cert(client, "P-01", BASE, BASE + 1440, 0.1)


def test_full_publish_lifecycle(client):
    _seed_day(client)
    r = client.post("/api/publications", json={"probe_id": "P-01", "day": DAY})
    assert r.status_code == 200
    pub = r.json()
    assert pub["status"] == "serving"
    payload = pub["payload"]
    assert payload["uncalibrated_minutes"] == []
    # segment [BASE+1, BASE+4); minute BASE below, gap at BASE+4, BASE+5 above
    # but BASE+6 calibrated = 8.0 (not > 8) so second segment is [BASE+5,BASE+6)
    assert [(s["start_minute"], s["end_minute"]) for s in payload["segments"]] == [
        (BASE + 1, BASE + 4),
        (BASE + 5, BASE + 6),
    ]

    # query shows serving
    q = client.get("/api/publications?probe_id=P-01").json()["publications"]
    assert len(q) == 1 and q[0]["status"] == "serving"

    # correction moves the reading at BASE+1 below threshold -> publication stale
    r2 = import_csv(client, CSV_HEADER + f"P-01,{BASE + 1},7.0\n")
    assert r2.json()["stale_publications"] == 1
    q2 = client.get("/api/publications?probe_id=P-01").json()["publications"]
    assert q2[0]["status"] == "stale"
    # old version still fully queryable
    assert q2[0]["payload"]["segments"][0]["start_minute"] == BASE + 1

    # recompute: serving replaced, old row becomes superseded in history
    r3 = client.post("/api/publications", json={"probe_id": "P-01", "day": DAY})
    assert r3.json()["status"] == "serving"
    new_segs = r3.json()["payload"]["segments"]
    assert new_segs[0]["start_minute"] == BASE + 2  # BASE+1 now cal 7.1
    latest = client.get("/api/publications?probe_id=P-01").json()["publications"]
    assert len(latest) == 1 and latest[0]["status"] == "serving"
    history = client.get(
        "/api/publications?probe_id=P-01&include_history=true"
    ).json()["publications"]
    statuses = sorted(h["status"] for h in history)
    assert statuses == ["serving", "superseded"]


def test_certificate_gap_truncates_and_lists_uncalibrated(client):
    import_csv(
        client,
        CSV_HEADER
        + f"P-01,{BASE},9.0\nP-01,{BASE + 1},9.0\n"
          f"P-01,{BASE + 2},9.0\nP-01,{BASE + 3},9.0\n",
    )
    # gap at minute BASE+2: [BASE,BASE+2) U [BASE+3,BASE+4)
    add_cert(client, "P-01", BASE, BASE + 2, 0.0)
    add_cert(client, "P-01", BASE + 3, BASE + 4, 0.0)
    pub = client.post(
        "/api/publications", json={"probe_id": "P-01", "day": DAY}
    ).json()
    payload = pub["payload"]
    assert payload["uncalibrated_minutes"] == [BASE + 2]
    assert [(s["start_minute"], s["end_minute"]) for s in payload["segments"]] == [
        (BASE, BASE + 2),
        (BASE + 3, BASE + 4),
    ]


def test_backdated_certificate_marks_publication_stale(client):
    import_csv(client, CSV_HEADER + f"P-01,{BASE},9.0\n")
    # no cert -> uncalibrated, no segments, but still publishable
    pub = client.post(
        "/api/publications", json={"probe_id": "P-01", "day": DAY}
    ).json()
    assert pub["payload"]["uncalibrated_minutes"] == [BASE]
    assert pub["payload"]["segments"] == []

    r = add_cert(client, "P-01", BASE, BASE + 1, 0.5)
    assert r.json()["stale_publications"] == 1
    q = client.get("/api/publications?probe_id=P-01").json()["publications"][0]
    assert q["status"] == "stale"  # history judgment changed by backdated cert

    repub = client.post(
        "/api/publications", json={"probe_id": "P-01", "day": DAY}
    ).json()
    assert repub["status"] == "serving"
    assert repub["payload"]["segments"][0]["max_calibrated_c"] == 9.5


def test_publish_without_readings_is_404_and_keeps_old_version(client):
    _seed_day(client)
    client.post("/api/publications", json={"probe_id": "P-01", "day": DAY})
    r = client.post(
        "/api/publications", json={"probe_id": "GHOST", "day": DAY}
    )
    assert r.status_code == 404
    # existing serving version untouched
    q = client.get("/api/publications?probe_id=P-01").json()["publications"][0]
    assert q["status"] == "serving"


def test_recompute_failure_keeps_stale_old_version(service_db, monkeypatch):
    # service-level fault injection: compute raises -> whole publish rolls back
    services.import_readings(CSV_HEADER + f"P-01,{BASE},9.0\n")
    services.add_certificate("P-01", BASE, BASE + 1, 0.0)
    ok = services.publish_day("P-01", BASE // 1440)
    assert ok["status"] == "serving"
    services.import_readings(CSV_HEADER + f"P-01,{BASE},9.5\n")  # mark stale

    def boom(*_a, **_kw):
        raise services.ComputeError("injected failure")

    monkeypatch.setattr(services, "compute_day", boom)
    try:
        services.publish_day("P-01", BASE // 1440)
        raise AssertionError("publish should have failed")
    except services.ServiceError as e:
        assert e.status_code == 500

    # no new row; stale old version still queryable
    import sqlite3

    conn = sqlite3.connect(service_db.get_db_path())
    rows = conn.execute(
        "SELECT id, status, data_version FROM publications"
    ).fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0][1] == "stale"


def test_recompute_all_reports_per_day_results(client):
    other = BASE + 72000  # 50 days later, different UTC day
    import_csv(
        client,
        CSV_HEADER + f"P-01,{BASE},9.0\nP-02,{other},8.5\n",
    )
    add_cert(client, "P-01", BASE, BASE + 1, 0.0)
    add_cert(client, "P-02", other, other + 1, 0.0)
    r = client.post("/api/publications/recompute-all")
    assert r.status_code == 200
    assert r.json()["total"] == 2 and r.json()["failed"] == 0
