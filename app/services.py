"""Business services: CSV import, calibration certificates, publishing,
and two-phase reading time-shifts (preview -> confirm).

Every mutating service runs inside :func:`app.db.write_tx`, i.e. it holds the
global writer lock and a single ``BEGIN IMMEDIATE`` transaction. On any error
the transaction is rolled back: no reading/certificate row, no version bump
and no stale flag survives a failed import or certificate change.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid

from . import db
from .engine import Calibration, ComputeError, Reading, compute_day
from .timeutil import day_bounds, day_to_iso, minute_to_iso, parse_utc_minute

PROBE_ALIASES = ("probe_id", "probe", "probeid", "sensor_id", "sensor", "探头号")
MINUTE_ALIASES = (
    "minute_utc",
    "minute",
    "utc_minute",
    "timestamp",
    "time",
    "ts",
    "utc分钟",
)
TEMP_ALIASES = (
    "temperature_c",
    "temperature",
    "temp",
    "temp_c",
    "温度",
)


class ServiceError(Exception):
    """Validation / conflict error -> HTTP 400/404/409."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# CSV import (corrections replace the original reading at probe + minute)
# --------------------------------------------------------------------------- #
def _norm(header: str) -> str:
    return header.strip().lower().lstrip("﻿")


def parse_readings_csv(text: str) -> list[tuple[str, int, float]]:
    """Parse an upload into deduplicated ``(probe, minute, temperature)`` rows.

    Later rows for the same probe/minute win, matching the correction rule.
    Raises :class:`ServiceError` (HTTP 400) on any malformed content.
    """
    if not text or not text.strip():
        raise ServiceError("CSV 文件为空")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ServiceError("CSV 缺少表头行") from exc

    header = [_norm(h) for h in header]
    col: dict[str, int] = {}
    for idx, name in enumerate(header):
        if name in PROBE_ALIASES:
            col["probe"] = idx
        elif name in MINUTE_ALIASES:
            col["minute"] = idx
        elif name in TEMP_ALIASES:
            col["temp"] = idx
    missing = [k for k in ("probe", "minute", "temp") if k not in col]
    if missing:
        raise ServiceError(
            "CSV 表头必须包含探头号、UTC 分钟、温度三列 "
            f"(probe_id, minute_utc, temperature_c)；缺少: {','.join(missing)}"
        )

    rows: dict[tuple[str, int], float] = {}
    seen = 0
    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(not cell.strip() for cell in raw):
            continue  # tolerate blank lines
        if len(raw) <= max(col.values()):
            raise ServiceError(f"第 {line_no} 行列数不足: {raw!r}")
        probe = raw[col["probe"]].strip()
        if not probe:
            raise ServiceError(f"第 {line_no} 行探头号为空")
        try:
            minute = parse_utc_minute(raw[col["minute"]])
        except ValueError as exc:
            raise ServiceError(f"第 {line_no} 行 UTC 分钟无效: {exc}") from exc
        temp_raw = raw[col["temp"]].strip()
        try:
            temp = float(temp_raw)
        except ValueError as exc:
            raise ServiceError(
                f"第 {line_no} 行温度不是数字: {temp_raw!r}"
            ) from exc
        if temp != temp or temp in (float("inf"), float("-inf")):
            raise ServiceError(f"第 {line_no} 行温度必须是有限数值")
        rows[(probe, minute)] = temp
        seen += 1

    if not rows:
        raise ServiceError("CSV 没有任何有效数据行")
    return [(probe, minute, temp) for (probe, minute), temp in rows.items()], seen


def import_readings(text: str) -> dict:
    """Upsert readings; corrected (probe, minute) rows replace old readings."""
    parsed, raw_rows = parse_readings_csv(text)
    with db.write_tx() as conn:
        version = db.bump_version(conn)
        inserted = replaced = 0
        affected_minutes: dict[str, set[int]] = {}
        for probe, minute, temp in parsed:
            existing = conn.execute(
                "SELECT 1 FROM readings WHERE probe_id = ? AND minute_utc = ?",
                (probe, minute),
            ).fetchone()
            conn.execute(
                "INSERT INTO readings(probe_id, minute_utc, temperature_c, data_version) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(probe_id, minute_utc) DO UPDATE SET "
                "temperature_c = excluded.temperature_c, data_version = excluded.data_version",
                (probe, minute, temp, version),
            )
            if existing:
                replaced += 1
            else:
                inserted += 1
            affected_minutes.setdefault(probe, set()).add(minute)

        stale = 0
        stale_by_probe: dict[str, list[int]] = {}
        for probe, minutes in affected_minutes.items():
            days = db.stale_days_for_minutes(conn, probe, list(minutes))
            stale += db.mark_days_stale(conn, probe, days)
            stale_by_probe[probe] = days

        return {
            "status": "committed",
            "data_version": version,
            "rows_in_file": raw_rows,
            "rows_applied": len(parsed),
            "inserted": inserted,
            "replaced": replaced,
            "stale_publications": stale,
            "stale_days_by_probe": {
                p: [day_to_iso(d) for d in days] for p, days in stale_by_probe.items()
            },
        }


# --------------------------------------------------------------------------- #
# Calibration certificates (non-overlapping, half-open validity per probe)
# --------------------------------------------------------------------------- #
def _validate_interval(start: int, end: int) -> None:
    if start >= end:
        raise ServiceError(
            "校准有效期必须是非空半开区间 [valid_from, valid_to)，即 valid_from < valid_to"
        )


def _check_no_overlap(
    conn, probe_id: str, start: int, end: int, exclude_id: int | None = None
) -> None:
    rows = conn.execute(
        "SELECT id, valid_from, valid_to FROM calibrations WHERE probe_id = ?",
        (probe_id,),
    ).fetchall()
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        # Half-open intervals touch at an endpoint without overlap:
        # [a,b) and [b,c) are allowed.
        if start < row["valid_to"] and row["valid_from"] < end:
            raise ServiceError(
                f"与探头 {probe_id} 已有证书 #{row['id']} 的有效期 "
                f"[{row['valid_from']}, {row['valid_to']}) 重叠",
                status_code=409,
            )


def add_certificate(
    probe_id: str, valid_from: int, valid_to: int, offset_c: float
) -> dict:
    if not probe_id or not str(probe_id).strip():
        raise ServiceError("探头号不能为空")
    _validate_interval(valid_from, valid_to)
    if not isinstance(offset_c, (int, float)) or offset_c != offset_c:
        raise ServiceError("温度偏移必须是有限数值")

    with db.write_tx() as conn:
        _check_no_overlap(conn, probe_id, valid_from, valid_to)
        version = db.bump_version(conn)
        cur = conn.execute(
            "INSERT INTO calibrations(probe_id, valid_from, valid_to, offset_c, data_version) "
            "VALUES (?, ?, ?, ?, ?)",
            (probe_id, valid_from, valid_to, float(offset_c), version),
        )
        cert_id = cur.lastrowid
        days = db.stale_days_for_span(conn, probe_id, valid_from, valid_to)
        stale = db.mark_days_stale(conn, probe_id, days)
        return {
            "status": "committed",
            "certificate_id": cert_id,
            "data_version": version,
            "stale_publications": stale,
            "stale_days": [day_to_iso(d) for d in days],
        }


def update_certificate(
    cert_id: int,
    valid_from: int | None = None,
    valid_to: int | None = None,
    offset_c: float | None = None,
) -> dict:
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM calibrations WHERE id = ?", (cert_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(f"证书 #{cert_id} 不存在", status_code=404)

        new_from = row["valid_from"] if valid_from is None else valid_from
        new_to = row["valid_to"] if valid_to is None else valid_to
        new_offset = row["offset_c"] if offset_c is None else offset_c
        _validate_interval(new_from, new_to)
        if new_offset != new_offset:
            raise ServiceError("温度偏移必须是有限数值")
        _check_no_overlap(
            conn, row["probe_id"], new_from, new_to, exclude_id=cert_id
        )

        # Days affected by either the old or the new interval.
        old_days = set(
            db.stale_days_for_span(
                conn, row["probe_id"], row["valid_from"], row["valid_to"]
            )
        )
        version = db.bump_version(conn)
        conn.execute(
            "UPDATE calibrations SET valid_from = ?, valid_to = ?, "
            "offset_c = ?, data_version = ? WHERE id = ?",
            (new_from, new_to, float(new_offset), version, cert_id),
        )
        new_days = set(
            db.stale_days_for_span(conn, row["probe_id"], new_from, new_to)
        )
        days = sorted(old_days | new_days)
        stale = db.mark_days_stale(conn, row["probe_id"], days)
        return {
            "status": "committed",
            "certificate_id": cert_id,
            "data_version": version,
            "stale_publications": stale,
            "stale_days": [day_to_iso(d) for d in days],
        }


def delete_certificate(cert_id: int) -> dict:
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM calibrations WHERE id = ?", (cert_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(f"证书 #{cert_id} 不存在", status_code=404)
        days = db.stale_days_for_span(
            conn, row["probe_id"], row["valid_from"], row["valid_to"]
        )
        version = db.bump_version(conn)
        conn.execute("DELETE FROM calibrations WHERE id = ?", (cert_id,))
        stale = db.mark_days_stale(conn, row["probe_id"], days)
        return {
            "status": "committed",
            "certificate_id": cert_id,
            "data_version": version,
            "stale_publications": stale,
            "stale_days": [day_to_iso(d) for d in days],
        }


def list_certificates(probe_id: str | None = None) -> list[dict]:
    conn = db.connect()
    try:
        if probe_id:
            rows = conn.execute(
                "SELECT * FROM calibrations WHERE probe_id = ? "
                "ORDER BY valid_from, id",
                (probe_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM calibrations ORDER BY probe_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def _load_snapshot(conn, probe_id: str, day: int, version: int):
    start, end = day_bounds(day)
    reading_rows = conn.execute(
        "SELECT minute_utc, temperature_c FROM readings "
        "WHERE probe_id = ? AND minute_utc >= ? AND minute_utc < ? "
        "ORDER BY minute_utc",
        (probe_id, start, end),
    ).fetchall()
    cal_rows = conn.execute(
        "SELECT valid_from, valid_to, offset_c FROM calibrations "
        "WHERE probe_id = ? AND valid_from < ? AND valid_to > ? "
        "ORDER BY valid_from",
        (probe_id, end, start),
    ).fetchall()
    readings = [Reading(r["minute_utc"], r["temperature_c"]) for r in reading_rows]
    calibrations = [
        Calibration(r["valid_from"], r["valid_to"], r["offset_c"]) for r in cal_rows
    ]
    return readings, calibrations


def publish_day(probe_id: str, day: int) -> dict:
    """(Re)publish one probe/day against the latest committed snapshot.

    The whole computation + replacement is one transaction: on failure the
    previous serving (or stale) row stays queryable and no new row is created.
    """
    if not probe_id or not str(probe_id).strip():
        raise ServiceError("探头号不能为空")

    with db.write_tx() as conn:
        version = db.current_version(conn)
        readings, calibrations = _load_snapshot(conn, probe_id, day, version)
        if not readings:
            raise ServiceError(
                f"探头 {probe_id} 在 UTC 日 {day_to_iso(day)} 没有任何读数，无法发布",
                status_code=404,
            )
        try:
            payload = compute_day(probe_id, day, readings, calibrations)
        except ComputeError as exc:
            # Explicit failure path: transaction rolls back, old version kept.
            raise ServiceError(f"重算失败: {exc}", status_code=500) from exc
        payload["data_version"] = version

        # Replace the currently served version atomically.
        conn.execute(
            "UPDATE publications SET status = 'superseded' "
            "WHERE probe_id = ? AND day = ? AND status IN ('serving', 'stale')",
            (probe_id, day),
        )
        cur = conn.execute(
            "INSERT INTO publications(probe_id, day, data_version, status, payload) "
            "VALUES (?, ?, ?, 'serving', ?)",
            (probe_id, day, version, json.dumps(payload, ensure_ascii=False)),
        )
        return {
            "status": "serving",
            "publication_id": cur.lastrowid,
            "probe_id": probe_id,
            "day": day,
            "day_utc": day_to_iso(day),
            "data_version": version,
            "payload": payload,
        }


def publish_many(probe_id: str | None, day: int | None) -> dict:
    """Republish every probe/day pair that has readings.

    Each pair is its own transaction so one failure does not poison the rest;
    the response lists per-day outcomes.
    """
    conn = db.connect()
    try:
        if probe_id is not None and day is not None:
            targets = [(probe_id, day)]
        elif probe_id is not None:
            rows = conn.execute(
                "SELECT DISTINCT probe_id, minute_utc / 1440 AS day "
                "FROM readings WHERE probe_id = ? ORDER BY day",
                (probe_id,),
            ).fetchall()
            targets = [(r["probe_id"], r["day"]) for r in rows]
        elif day is not None:
            start, end = day_bounds(day)
            rows = conn.execute(
                "SELECT DISTINCT probe_id FROM readings "
                "WHERE minute_utc >= ? AND minute_utc < ? ORDER BY probe_id",
                (start, end),
            ).fetchall()
            targets = [(r["probe_id"], day) for r in rows]
        else:
            rows = conn.execute(
                "SELECT DISTINCT probe_id, minute_utc / 1440 AS day "
                "FROM readings ORDER BY probe_id, day"
            ).fetchall()
            targets = [(r["probe_id"], r["day"]) for r in rows]
    finally:
        conn.close()

    results = []
    for pid, d in targets:
        try:
            out = publish_day(pid, d)
            results.append(
                {
                    "probe_id": pid,
                    "day": day_to_iso(d),
                    "ok": True,
                    "publication_id": out["publication_id"],
                    "data_version": out["data_version"],
                }
            )
        except ServiceError as exc:
            results.append(
                {"probe_id": pid, "day": day_to_iso(d), "ok": False, "error": str(exc)}
            )
    return {"total": len(results), "succeeded": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]), "results": results}


# --------------------------------------------------------------------------- #
# Reading time-shift (clock correction): two-phase preview -> confirm
# --------------------------------------------------------------------------- #
def _shift_snapshot(
    conn, probe_id: str, start: int, end: int, offset: int
) -> tuple[list[tuple[int, float]], list[int]]:
    """Load the source readings and the blocking target minutes.

    ``source`` is every reading inside the half-open ``[start, end)`` window.
    ``blocking`` lists target minutes (``source minute + offset``) that are
    occupied by readings *outside* the window — those may never be
    overwritten. Target minutes occupied by other in-window readings are
    fine: the whole batch moves atomically.
    """
    rows = conn.execute(
        "SELECT minute_utc, temperature_c FROM readings "
        "WHERE probe_id = ? AND minute_utc >= ? AND minute_utc < ? "
        "ORDER BY minute_utc",
        (probe_id, start, end),
    ).fetchall()
    source = [(int(r["minute_utc"]), float(r["temperature_c"])) for r in rows]

    targets = {m + offset for m, _ in source}
    blocking: list[int] = []
    if targets:
        lo, hi = min(targets), max(targets)
        outside = conn.execute(
            "SELECT minute_utc FROM readings "
            "WHERE probe_id = ? AND minute_utc >= ? AND minute_utc <= ? "
            "AND (minute_utc < ? OR minute_utc >= ?) ORDER BY minute_utc",
            (probe_id, lo, hi, start, end),
        ).fetchall()
        blocking = [int(r["minute_utc"]) for r in outside
                    if int(r["minute_utc"]) in targets]
    return source, blocking


def _shift_fingerprint(
    probe_id: str,
    start: int,
    end: int,
    offset: int,
    source: list[tuple[int, float]],
    blocking: list[int],
) -> str:
    """Hash everything a confirm must re-verify against its preview."""
    blob = json.dumps(
        {
            "probe_id": probe_id,
            "start": start,
            "end": end,
            "offset": offset,
            "source": [[m, repr(t)] for m, t in source],
            "blocking": blocking,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _validate_shift_args(probe_id: str, start: int, end: int, offset: int) -> None:
    if not probe_id or not str(probe_id).strip():
        raise ServiceError("探头号不能为空")
    if start >= end:
        raise ServiceError(
            "原分钟区间必须是非空半开区间 [start_minute, end_minute)，"
            "即 start_minute < end_minute"
        )
    if offset == 0:
        raise ServiceError("偏移分钟不能为 0（无平移意义）")


class _FingerprintMismatch(Exception):
    """Internal: confirm observed data different from the preview snapshot."""


def preview_shift(probe_id: str, start: int, end: int, offset: int) -> dict:
    """Preview a batch time-shift and stash its fingerprint for confirmation.

    Runs inside the writer transaction so the recorded snapshot is a real
    committed state; it changes no readings and does not bump the version.
    """
    _validate_shift_args(probe_id, start, end, offset)
    with db.write_tx() as conn:
        source, blocking = _shift_snapshot(conn, probe_id, start, end, offset)
        if not source:
            raise ServiceError(
                f"探头 {probe_id} 在区间 [{start}, {end}) 内没有读数，无法校时",
                status_code=404,
            )
        fingerprint = _shift_fingerprint(
            probe_id, start, end, offset, source, blocking
        )
        token = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO shift_operations(token, probe_id, start_minute, "
            "end_minute, offset_minutes, fingerprint, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'previewed')",
            (token, probe_id, start, end, offset, fingerprint),
        )
        source_days = sorted({m // 1440 for m, _ in source})
        target_days = sorted({(m + offset) // 1440 for m, _ in source})
        return {
            "token": token,
            "status": "previewed",
            "probe_id": probe_id,
            "start_minute": start,
            "end_minute": end,
            "start_iso": minute_to_iso(start),
            "end_iso": minute_to_iso(end),
            "offset_minutes": offset,
            "reading_count": len(source),
            "items": [
                {
                    "minute_utc": m,
                    "minute_iso": minute_to_iso(m),
                    "new_minute_utc": m + offset,
                    "new_minute_iso": minute_to_iso(m + offset),
                    "temperature_c": t,
                }
                for m, t in source
            ],
            "conflicts": [
                {"minute_utc": b, "minute_iso": minute_to_iso(b)} for b in blocking
            ],
            "confirmable": not blocking,
            "source_days": [day_to_iso(d) for d in source_days],
            "target_days": [day_to_iso(d) for d in target_days],
        }


def confirm_shift(token: str) -> dict:
    """Confirm a previously previewed shift; moves the whole batch atomically.

    Refuses (HTTP 409) when:
      * the token was already confirmed (no double shift) or has expired;
      * the source readings or the target occupancy changed since the
        preview (fingerprint mismatch — the token is then expired);
      * target minutes are occupied by out-of-window readings.
    On any refusal nothing changes: readings, data_version and stale flags
    stay exactly as before (the transaction rolls back).
    """
    if not token or not str(token).strip():
        raise ServiceError("确认令牌不能为空")
    token = str(token).strip()

    try:
        with db.write_tx() as conn:
            rec = conn.execute(
                "SELECT * FROM shift_operations WHERE token = ?", (token,)
            ).fetchone()
            if rec is None:
                raise ServiceError(f"校时预览 {token} 不存在", status_code=404)
            if rec["status"] == "confirmed":
                raise ServiceError(
                    "该预览已确认过，不能重复平移；如需再次校时请重新预览",
                    status_code=409,
                )
            if rec["status"] == "expired":
                raise ServiceError(
                    "该预览已失效（预览后数据曾变化），请重新预览",
                    status_code=409,
                )

            probe_id = rec["probe_id"]
            start = int(rec["start_minute"])
            end = int(rec["end_minute"])
            offset = int(rec["offset_minutes"])

            source, blocking = _shift_snapshot(conn, probe_id, start, end, offset)
            fingerprint = _shift_fingerprint(
                probe_id, start, end, offset, source, blocking
            )
            if fingerprint != rec["fingerprint"]:
                raise _FingerprintMismatch(token)
            if blocking:
                raise ServiceError(
                    "目标分钟被区间外读数占用，整批迁移被拒绝"
                    "（不得覆盖区间外读数）: "
                    + ", ".join(minute_to_iso(b) for b in blocking),
                    status_code=409,
                )

            version = db.bump_version(conn)
            # Delete-then-insert inside one transaction: in-window readings may
            # legitimately land on minutes previously held by other in-window
            # readings, and the batch must move as one atomic unit.
            conn.execute(
                "DELETE FROM readings "
                "WHERE probe_id = ? AND minute_utc >= ? AND minute_utc < ?",
                (probe_id, start, end),
            )
            for m, t in source:
                conn.execute(
                    "INSERT INTO readings(probe_id, minute_utc, temperature_c, "
                    "data_version) VALUES (?, ?, ?, ?)",
                    (probe_id, m + offset, t, version),
                )

            # Both the original days and the target days are affected: their
            # serving publications go stale immediately (old versions stay
            # queryable). A source day left without readings can no longer be
            # republished (publish_day -> 404), so it stops serving for good.
            days = sorted(
                {m // 1440 for m, _ in source}
                | {(m + offset) // 1440 for m, _ in source}
            )
            stale = db.mark_days_stale(conn, probe_id, days)
            conn.execute(
                "UPDATE shift_operations SET status = 'confirmed', moved_rows = ?, "
                "data_version = ?, "
                "confirmed_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE token = ?",
                (len(source), version, token),
            )
            return {
                "status": "committed",
                "token": token,
                "probe_id": probe_id,
                "offset_minutes": offset,
                "moved_rows": len(source),
                "window": {
                    "from": [start, end],
                    "to": [start + offset, end + offset],
                    "from_iso": [minute_to_iso(start), minute_to_iso(end)],
                    "to_iso": [
                        minute_to_iso(start + offset),
                        minute_to_iso(end + offset),
                    ],
                },
                "data_version": version,
                "stale_publications": stale,
                "stale_days": [day_to_iso(d) for d in days],
            }
    except _FingerprintMismatch:
        # The migration itself changed nothing (the transaction above rolled
        # back). Expire the token in its own small transaction so this preview
        # can never be confirmed against a different data state later.
        with db.write_tx() as conn:
            conn.execute(
                "UPDATE shift_operations SET status = 'expired' "
                "WHERE token = ? AND status = 'previewed'",
                (token,),
            )
        raise ServiceError(
            "预览后源读数或目标占用已变化，本次确认被拒绝，请重新预览",
            status_code=409,
        )


def get_shift(token: str) -> dict:
    """Report the current state of a preview/confirmation token."""
    conn = db.connect()
    try:
        rec = conn.execute(
            "SELECT * FROM shift_operations WHERE token = ?", (token,)
        ).fetchone()
        if rec is None:
            raise ServiceError(f"校时预览 {token} 不存在", status_code=404)
        return {
            "token": rec["token"],
            "probe_id": rec["probe_id"],
            "start_minute": rec["start_minute"],
            "end_minute": rec["end_minute"],
            "start_iso": minute_to_iso(rec["start_minute"]),
            "end_iso": minute_to_iso(rec["end_minute"]),
            "offset_minutes": rec["offset_minutes"],
            "status": rec["status"],
            "moved_rows": rec["moved_rows"],
            "data_version": rec["data_version"],
            "created_at": rec["created_at"],
            "confirmed_at": rec["confirmed_at"],
        }
    finally:
        conn.close()
