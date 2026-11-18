"""Edge cases: UTC day boundaries, negative offsets, multiple probes, gaps."""
from app.engine import Calibration, Reading, compute_day
from app.timeutil import day_bounds, parse_day, parse_utc_minute, day_to_iso

from conftest import CSV_HEADER, add_cert, import_csv

DAY = "2024-01-01"
BASE = parse_day(DAY) * 1440


def test_day_helpers_roundtrip():
    idx = parse_day("2024-01-01")
    assert day_to_iso(idx) == "2024-01-01"
    start, end = day_bounds(idx)
    assert end - start == 1440
    assert parse_utc_minute("2024-01-01T00:00Z") == start
    assert parse_utc_minute("2024-01-01T23:59Z") == end - 1
    assert parse_utc_minute(start) == start  # numeric epoch minute
    # next day
    assert day_to_iso(idx + 1) == "2024-01-02"


def test_day_boundary_minutes_split_across_days(client):
    # one reading at 23:59, one at next day 00:00
    csv = (
        CSV_HEADER
        + f"P-09,{BASE + 1439},9.5\n"
        + f"P-09,{BASE + 1440},9.6\n"
    )
    assert import_csv(client, csv).status_code == 200
    add_cert(client, "P-09", BASE, BASE + 2880, 0.0)
    p1 = client.post(
        "/api/publications", json={"probe_id": "P-09", "day": DAY}
    ).json()
    p2 = client.post(
        "/api/publications", json={"probe_id": "P-09", "day": "2024-01-02"}
    ).json()
    # each day sees only its own single minute, no cross-day segment
    assert p1["payload"]["segments"][0]["end_minute"] == BASE + 1440
    assert p2["payload"]["segments"][0]["start_minute"] == BASE + 1440
    assert p1["payload"]["segments"][0]["end_minute"] != p2["payload"]["segments"][0]["end_minute"]


def test_negative_offset_can_remove_false_alarm(client):
    import_csv(client, CSV_HEADER + f"P-10,{BASE},9.0\n")
    add_cert(client, "P-10", BASE, BASE + 1, -1.5)  # 9.0 - 1.5 = 7.5
    pub = client.post(
        "/api/publications", json={"probe_id": "P-10", "day": DAY}
    ).json()
    assert pub["payload"]["segments"] == []
    assert pub["payload"]["calibrated_minutes"] == 1


def test_query_missing_publication_404_and_empty_list(client):
    r = client.get("/api/publications?probe_id=NOPE&day=2024-01-01")
    assert r.status_code == 200 and r.json()["count"] == 0
    r2 = client.get("/api/publications/NOPE/2024-01-01")
    assert r2.status_code == 404


def test_two_probes_have_independent_lifecycles(client):
    import_csv(
        client,
        CSV_HEADER + f"P-A,{BASE},9.0\nP-B,{BASE},9.0\n",
    )
    add_cert(client, "P-A", BASE, BASE + 1, 0.0)
    add_cert(client, "P-B", BASE, BASE + 1, 0.0)
    client.post("/api/publications", json={"probe_id": "P-A", "day": DAY})
    client.post("/api/publications", json={"probe_id": "P-B", "day": DAY})

    # correcting P-A stales only P-A's publication
    import_csv(client, CSV_HEADER + f"P-A,{BASE},5.0\n")
    rows = client.get("/api/publications").json()["publications"]
    status = {r["probe_id"]: r["status"] for r in rows}
    assert status == {"P-A": "stale", "P-B": "serving"}


def test_engine_non_contiguous_hot_minutes_open_separate_runs():
    # hot at BASE (run opens), cold at BASE+1 (closes it),
    # hot again at BASE+2 and BASE+3 (new run), then end of day.
    readings = [
        Reading(BASE, 9.0),
        Reading(BASE + 1, 5.0),
        Reading(BASE + 2, 8.4),
        Reading(BASE + 3, 8.6),
    ]
    payload = compute_day(
        "P", BASE // 1440, readings, [Calibration(BASE, BASE + 4, 0.0)]
    )
    assert [(s["start_minute"], s["end_minute"]) for s in payload["segments"]] == [
        (BASE, BASE + 1),
        (BASE + 2, BASE + 4),
    ]


def test_engine_missing_minute_breaks_run():
    readings = [Reading(BASE, 9.0), Reading(BASE + 2, 9.0)]  # gap at BASE+1
    payload = compute_day(
        "P", BASE // 1440, readings, [Calibration(BASE, BASE + 3, 0.0)]
    )
    assert [(s["start_minute"], s["end_minute"]) for s in payload["segments"]] == [
        (BASE, BASE + 1),
        (BASE + 2, BASE + 3),
    ]


def test_certificate_query_filter_and_iso_rendering(client):
    add_cert(client, "P-X", BASE, BASE + 60, 0.0)
    r = client.get("/api/certificates?probe_id=P-X")
    cert = r.json()["certificates"][0]
    assert cert["valid_from_iso"] == "2024-01-01T00:00Z"
    assert cert["valid_to_iso"] == "2024-01-01T01:00Z"
    assert client.get("/api/certificates?probe_id=OTHER").json()["count"] == 0


def test_readings_endpoint_day_filter(client):
    import_csv(
        client,
        CSV_HEADER + f"P-Z,{BASE},1.0\nP-Z,{BASE + 1440},2.0\n",
    )
    today = client.get("/api/readings?probe_id=P-Z&day=2024-01-01").json()
    tomorrow = client.get("/api/readings?probe_id=P-Z&day=2024-01-02").json()
    assert today["count"] == 1 and tomorrow["count"] == 1
    assert today["readings"][0]["minute_iso"] == "2024-01-01T00:00Z"
