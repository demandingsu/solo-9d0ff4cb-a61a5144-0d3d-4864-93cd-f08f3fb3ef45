"""读数校时（整批分钟平移）测试。

覆盖：跨日迁移与双日过期、区间内相互占用允许、区间外目标冲突拒绝、
预览后源读数/目标占用变化拒绝、重复确认不二次平移、失败不改状态、
与发布交错时的一致性、并发确认串行化。
"""
import threading

import pytest

from app import db, services
from app.timeutil import parse_day

from conftest import CSV_HEADER, add_cert, import_csv

DAY = "2024-01-01"
DAY2 = "2024-01-02"
BASE = parse_day(DAY) * 1440  # 2024-01-01T00:00Z epoch minutes
DAY_IDX = BASE // 1440


def _preview(client, probe, start, end, offset):
    return client.post(
        "/api/readings/shift/preview",
        json={
            "probe_id": probe,
            "start_minute": start,
            "end_minute": end,
            "offset_minutes": offset,
        },
    )


def _confirm(client, token):
    return client.post("/api/readings/shift/confirm", json={"token": token})


def _minutes(client, probe):
    rows = client.get(f"/api/readings?probe_id={probe}").json()["readings"]
    return {r["minute_utc"]: r["temperature_c"] for r in rows}


def _version(client):
    return client.get("/health").json()["data_version"]


def _svc_version():
    conn = db.connect()
    try:
        return db.current_version(conn)
    finally:
        conn.close()


def _pub_status(client, probe, day):
    pubs = client.get(
        f"/api/publications?probe_id={probe}&day={day}"
    ).json()["publications"]
    return pubs[0]["status"] if pubs else None


# --------------------------------------------------------------------------- #
# 跨日迁移 + 过期语义
# --------------------------------------------------------------------------- #
def test_shift_cross_day_stales_both_days_and_empties_source_day(client):
    # 两条读数在原日 23:58/23:59，+2 分钟后落入次日
    import_csv(client, CSV_HEADER + f"P-01,{BASE + 1438},8.5\nP-01,{BASE + 1439},9.0\n")
    add_cert(client, "P-01", BASE, BASE + 2880, 0.0)
    client.post("/api/publications", json={"probe_id": "P-01", "day": DAY})
    assert _pub_status(client, "P-01", DAY) == "serving"

    prev = _preview(client, "P-01", BASE + 1438, BASE + 1440, 2)
    assert prev.status_code == 200
    body = prev.json()
    assert body["confirmable"] is True and body["conflicts"] == []
    assert body["reading_count"] == 2
    assert body["source_days"] == [DAY] and body["target_days"] == [DAY2]
    assert body["items"][0]["new_minute_utc"] == BASE + 1440
    assert body["items"][0]["new_minute_iso"] == DAY2 + "T00:00Z"

    v0 = _version(client)
    conf = _confirm(client, body["token"])
    assert conf.status_code == 200
    out = conf.json()
    assert out["moved_rows"] == 2
    assert out["data_version"] == v0 + 1  # 迁移是一次数据变更
    assert out["stale_days"] == [DAY, DAY2]

    # 读数整体跨日落位，原分钟已不存在
    assert _minutes(client, "P-01") == {BASE + 1440: 8.5, BASE + 1441: 9.0}

    # 原日 serving -> stale（旧版仍可查，payload 原样保留）
    pub = client.get("/api/publications?probe_id=P-01&day=" + DAY).json()
    old = pub["publications"][0]
    assert old["status"] == "stale"
    assert old["payload"]["total_reading_minutes"] == 2

    # 原日读数已迁空：不得继续供数——重算 404，stale 旧版保留
    r = client.post("/api/publications", json={"probe_id": "P-01", "day": DAY})
    assert r.status_code == 404
    still = client.get("/api/publications?probe_id=P-01&day=" + DAY).json()
    assert still["publications"][0]["status"] == "stale"
    assert still["publications"][0]["payload"]["total_reading_minutes"] == 2

    # 新日可正常发布，内容只来自迁移后的已提交读数
    r2 = client.post("/api/publications", json={"probe_id": "P-01", "day": DAY2})
    assert r2.status_code == 200
    assert r2.json()["payload"]["total_reading_minutes"] == 2
    assert r2.json()["payload"]["segments"][0]["start_minute"] == BASE + 1440


def test_shift_marks_existing_target_day_publication_stale(client):
    # 原日、新日各有 serving 发布；迁移后两日都过期
    import_csv(
        client,
        CSV_HEADER + f"P-07,{BASE + 100},8.5\nP-07,{BASE + 1440},9.0\n",
    )
    add_cert(client, "P-07", BASE, BASE + 2880, 0.0)
    client.post("/api/publications", json={"probe_id": "P-07", "day": DAY})
    client.post("/api/publications", json={"probe_id": "P-07", "day": DAY2})

    prev = _preview(client, "P-07", BASE + 1440, BASE + 1441, -1440).json()
    assert prev["confirmable"] is True  # 目标 BASE+0 无读数
    conf = _confirm(client, prev["token"])
    assert conf.status_code == 200
    assert conf.json()["stale_days"] == [DAY, DAY2]
    assert _pub_status(client, "P-07", DAY) == "stale"
    assert _pub_status(client, "P-07", DAY2) == "stale"

    # 次日读数迁空 -> 重算 404；原日重算后只含当前已提交的两条读数
    assert client.post(
        "/api/publications", json={"probe_id": "P-07", "day": DAY2}
    ).status_code == 404
    r = client.post("/api/publications", json={"probe_id": "P-07", "day": DAY})
    assert r.status_code == 200
    assert r.json()["payload"]["total_reading_minutes"] == 2
    assert _minutes(client, "P-07") == {BASE: 9.0, BASE + 100: 8.5}


# --------------------------------------------------------------------------- #
# 占用规则
# --------------------------------------------------------------------------- #
def test_shift_intra_window_overlap_allowed(client):
    # 分钟 0,1,2 整体 +1 -> 目标 1,2,3；1,2 原本被区间内读数占用，允许
    import_csv(
        client,
        CSV_HEADER
        + f"P-02,{BASE},8.1\nP-02,{BASE + 1},8.2\nP-02,{BASE + 2},8.3\n",
    )
    prev = _preview(client, "P-02", BASE, BASE + 3, 1).json()
    assert prev["confirmable"] is True and prev["conflicts"] == []
    conf = _confirm(client, prev["token"])
    assert conf.status_code == 200 and conf.json()["moved_rows"] == 3
    assert _minutes(client, "P-02") == {
        BASE + 1: 8.1,
        BASE + 2: 8.2,
        BASE + 3: 8.3,
    }


def test_shift_conflict_with_outside_reading_rejected_and_atomic(client):
    # 区间外分钟 BASE+5 有读数；目标分钟 BASE+5 被占用 -> 冲突
    import_csv(
        client,
        CSV_HEADER
        + f"P-03,{BASE},8.1\nP-03,{BASE + 1},8.2\nP-03,{BASE + 5},9.9\n",
    )
    add_cert(client, "P-03", BASE, BASE + 10, 0.0)
    client.post("/api/publications", json={"probe_id": "P-03", "day": DAY})

    prev = _preview(client, "P-03", BASE, BASE + 2, 5).json()
    assert prev["confirmable"] is False
    assert [c["minute_utc"] for c in prev["conflicts"]] == [BASE + 5]

    v0 = _version(client)
    conf = _confirm(client, prev["token"])
    assert conf.status_code == 409
    assert "不得覆盖区间外读数" in conf.json()["error"]

    # 迁移失败：读数、版本号、过期标记全部保持原状
    assert _minutes(client, "P-03") == {
        BASE: 8.1,
        BASE + 1: 8.2,
        BASE + 5: 9.9,
    }
    assert _version(client) == v0
    assert _pub_status(client, "P-03", DAY) == "serving"


def test_shift_never_overwrites_even_when_window_empties(client):
    # 区间内读数全部迁走，落点恰是另一条区间外读数 -> 仍拒绝
    import_csv(
        client,
        CSV_HEADER + f"P-06,{BASE},8.1\nP-06,{BASE + 60},7.7\n",
    )
    prev = _preview(client, "P-06", BASE, BASE + 1, 60).json()
    assert prev["confirmable"] is False
    assert [c["minute_utc"] for c in prev["conflicts"]] == [BASE + 60]
    assert _confirm(client, prev["token"]).status_code == 409
    assert _minutes(client, "P-06") == {BASE: 8.1, BASE + 60: 7.7}


# --------------------------------------------------------------------------- #
# 预览后变化 -> 拒绝确认
# --------------------------------------------------------------------------- #
def test_confirm_rejected_when_source_changes_after_preview(client):
    import_csv(client, CSV_HEADER + f"P-04,{BASE},8.1\nP-04,{BASE + 1},8.2\n")
    prev = _preview(client, "P-04", BASE, BASE + 2, 10).json()

    # 预览后更正区间内一条读数（温度变化）
    import_csv(client, CSV_HEADER + f"P-04,{BASE},7.7\n")

    v0 = _version(client)
    conf = _confirm(client, prev["token"])
    assert conf.status_code == 409
    assert "重新预览" in conf.json()["error"]
    # 令牌被置为 expired，之后任何确认都拒绝
    assert client.get(f"/api/readings/shift/{prev['token']}").json()[
        "status"
    ] == "expired"
    assert _confirm(client, prev["token"]).status_code == 409
    # 读数未被平移
    assert _minutes(client, "P-04") == {BASE: 7.7, BASE + 1: 8.2}
    assert _version(client) == v0


def test_confirm_rejected_when_target_occupancy_changes(client):
    import_csv(client, CSV_HEADER + f"P-05,{BASE},8.1\nP-05,{BASE + 1},8.2\n")
    prev = _preview(client, "P-05", BASE, BASE + 2, 10).json()
    assert prev["confirmable"] is True

    # 预览后在目标分钟（区间外）新入一条读数
    import_csv(client, CSV_HEADER + f"P-05,{BASE + 10},6.6\n")

    conf = _confirm(client, prev["token"])
    assert conf.status_code == 409
    assert _minutes(client, "P-05") == {
        BASE: 8.1,
        BASE + 1: 8.2,
        BASE + 10: 6.6,
    }


# --------------------------------------------------------------------------- #
# 重复确认
# --------------------------------------------------------------------------- #
def test_double_confirm_does_not_shift_twice(client):
    import_csv(client, CSV_HEADER + f"P-08,{BASE},8.5\n")
    prev = _preview(client, "P-08", BASE, BASE + 1, 5).json()

    c1 = _confirm(client, prev["token"])
    assert c1.status_code == 200 and c1.json()["moved_rows"] == 1
    v1 = _version(client)

    c2 = _confirm(client, prev["token"])
    assert c2.status_code == 409
    assert "重复" in c2.json()["error"]
    # 没有二次平移：读数停在 BASE+5，版本号不再增长
    assert _minutes(client, "P-08") == {BASE + 5: 8.5}
    assert _version(client) == v1
    assert client.get(f"/api/readings/shift/{prev['token']}").json()[
        "status"
    ] == "confirmed"


def test_concurrent_confirm_only_one_succeeds(service_db):
    services.import_readings(CSV_HEADER + f"P-09,{BASE},8.5\n")
    prev = services.preview_shift("P-09", BASE, BASE + 1, 10)

    results = []

    def worker():
        try:
            services.confirm_shift(prev["token"])
            results.append("ok")
        except services.ServiceError as exc:
            results.append(exc.status_code)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1
    assert results.count(409) == 4
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT minute_utc FROM readings WHERE probe_id = 'P-09'"
        ).fetchall()
        assert [r["minute_utc"] for r in rows] == [BASE + 10]  # 只平移一次
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def test_shift_validation_errors(client):
    import_csv(client, CSV_HEADER + f"P-10,{BASE},8.5\n")
    # start >= end
    assert _preview(client, "P-10", BASE + 1, BASE + 1, 5).status_code == 400
    assert _preview(client, "P-10", BASE + 2, BASE + 1, 5).status_code == 400
    # 偏移为 0
    assert _preview(client, "P-10", BASE, BASE + 1, 0).status_code == 400
    # 空区间（无读数）
    r = _preview(client, "P-10", BASE + 100, BASE + 200, 5)
    assert r.status_code == 404
    # 不存在的探头
    assert _preview(client, "GHOST", BASE, BASE + 1, 5).status_code == 404
    # 不存在的令牌
    assert _confirm(client, "no-such-token").status_code == 404
    assert client.get("/api/readings/shift/no-such-token").status_code == 404
    # 非法时间字符串
    r = client.post(
        "/api/readings/shift/preview",
        json={
            "probe_id": "P-10",
            "start_minute": "not-a-time",
            "end_minute": BASE + 1,
            "offset_minutes": 5,
        },
    )
    assert r.status_code == 400
    # 校验失败不产生任何记录/版本变化
    assert _minutes(client, "P-10") == {BASE: 8.5}
    assert _version(client) == 1


def test_shift_preview_accepts_iso_window_and_negative_offset(client):
    import_csv(client, CSV_HEADER + f"P-11,{BASE + 30},8.5\n")
    r = _preview(
        client, "P-11", "2024-01-01T00:10Z", "2024-01-01T01:00Z", -30
    )
    assert r.status_code == 200
    body = r.json()
    assert body["start_minute"] == BASE + 10
    assert body["items"][0]["new_minute_utc"] == BASE
    conf = _confirm(client, body["token"])
    assert conf.status_code == 200
    assert _minutes(client, "P-11") == {BASE: 8.5}


# --------------------------------------------------------------------------- #
# 与发布交错的一致性（service 层）
# --------------------------------------------------------------------------- #
def test_interleaved_shift_and_publish_use_one_committed_snapshot(service_db):
    services.import_readings(
        CSV_HEADER + f"P-12,{BASE},8.5\nP-12,{BASE + 1},9.0\n"
    )
    services.add_certificate("P-12", BASE, BASE + 200, 0.0)
    p1 = services.publish_day("P-12", DAY_IDX)
    assert p1["status"] == "serving"

    # 预览 -> 与一次导入交错（区间外，不影响指纹）-> 确认 -> 重算
    prev = services.preview_shift("P-12", BASE, BASE + 2, 100)
    services.import_readings(CSV_HEADER + f"P-12,{BASE + 50},7.0\n")
    out = services.confirm_shift(prev["token"])
    assert out["status"] == "committed"

    p2 = services.publish_day("P-12", DAY_IDX)
    assert p2["status"] == "serving"
    # 新版本只依据同一已提交状态：迁移后的两条 + 交错导入的一条
    assert p2["payload"]["total_reading_minutes"] == 3
    assert p2["payload"]["segments"][0]["start_minute"] == BASE + 100
    assert p2["payload"]["segments"][0]["end_minute"] == BASE + 102

    # 预览后区间内变化则确认必须失败，且读数/版本/过期标记不变
    prev2 = services.preview_shift("P-12", BASE + 100, BASE + 102, 5)
    v0 = _svc_version()
    services.import_readings(CSV_HEADER + f"P-12,{BASE + 100},1.0\n")
    with pytest.raises(services.ServiceError) as ei:
        services.confirm_shift(prev2["token"])
    assert ei.value.status_code == 409
    conn = db.connect()
    try:
        assert db.current_version(conn) != v0  # 导入本身已 +1
        v1 = db.current_version(conn)
        temps = dict(
            conn.execute(
                "SELECT minute_utc, temperature_c FROM readings "
                "WHERE probe_id = 'P-12'"
            ).fetchall()
        )
        assert temps[BASE + 100] == 1.0  # 更正生效但未被平移
        assert BASE + 105 not in temps
    finally:
        conn.close()
    with pytest.raises(services.ServiceError):
        services.confirm_shift(prev2["token"])  # 已 expired
    assert _svc_version() == v1  # 失败确认不增版本
