"""FastAPI application: calibration dataset, certificates and publications."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, services
from .timeutil import (
    day_bounds,
    day_to_iso,
    minute_to_iso,
    parse_day,
    parse_utc_minute,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="冷链探头校准数据集",
    description=(
        "探头读数导入（同探头同分钟更正替换）、半开非重叠校准证书管理、"
        "按探头/UTC 日发布校准后超温连续时段，并维护版本与过期状态。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(services.ServiceError)
async def _service_error_handler(_: Request, exc: services.ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc)})


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class CertificateIn(BaseModel):
    probe_id: str = Field(..., description="探头号")
    valid_from: str | int = Field(..., description="有效期起（含），UTC 分钟或 ISO-8601")
    valid_to: str | int = Field(..., description="有效期止（不含），UTC 分钟或 ISO-8601")
    offset_c: float = Field(..., description="校准温度偏移（摄氏度，加到原始读数上）")


class CertificatePatch(BaseModel):
    valid_from: Optional[str | int] = None
    valid_to: Optional[str | int] = None
    offset_c: Optional[float] = None


class PublishIn(BaseModel):
    probe_id: str
    day: str | int = Field(..., description="UTC 日，YYYY-MM-DD 或日序号")


# --------------------------------------------------------------------------- #
# Health & metadata
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    conn = db.connect()
    try:
        version = db.current_version(conn)
    finally:
        conn.close()
    return {"status": "ok", "data_version": version}


# --------------------------------------------------------------------------- #
# Readings
# --------------------------------------------------------------------------- #
@app.post("/api/readings/import")
async def import_readings(
    file: UploadFile | None = File(default=None),
    csv_text: str | None = Form(default=None),
) -> dict:
    """Upload a probe/minute/temperature CSV.

    Same probe + same UTC minute rows replace the previous reading
    (both within the file and against stored data).
    """
    if file is not None:
        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise services.ServiceError(f"CSV 必须是 UTF-8 编码: {exc}") from exc
    elif csv_text is not None:
        text = csv_text
    else:
        raise services.ServiceError(
            "请以 multipart 字段 file 上传 CSV，或在 csv_text 字段提供 CSV 文本"
        )
    return services.import_readings(text)


@app.get("/api/readings")
def list_readings(probe_id: str, day: Optional[str] = None) -> dict:
    conn = db.connect()
    try:
        if day is not None:
            d = parse_day(day)
            start, end = day_bounds(d)
            rows = conn.execute(
                "SELECT probe_id, minute_utc, temperature_c, data_version "
                "FROM readings WHERE probe_id = ? AND minute_utc >= ? AND minute_utc < ? "
                "ORDER BY minute_utc",
                (probe_id, start, end),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT probe_id, minute_utc, temperature_c, data_version "
                "FROM readings WHERE probe_id = ? ORDER BY minute_utc",
                (probe_id,),
            ).fetchall()
        return {
            "probe_id": probe_id,
            "day": day,
            "count": len(rows),
            "readings": [
                {
                    "minute_utc": r["minute_utc"],
                    "minute_iso": minute_to_iso(r["minute_utc"]),
                    "temperature_c": r["temperature_c"],
                    "data_version": r["data_version"],
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Calibration certificates
# --------------------------------------------------------------------------- #
@app.post("/api/certificates")
def add_certificate(body: CertificateIn) -> dict:
    return services.add_certificate(
        body.probe_id,
        parse_utc_minute(body.valid_from),
        parse_utc_minute(body.valid_to),
        body.offset_c,
    )


@app.get("/api/certificates")
def get_certificates(probe_id: Optional[str] = None) -> dict:
    rows = services.list_certificates(probe_id)
    return {
        "count": len(rows),
        "certificates": [
            {
                **r,
                "valid_from_iso": minute_to_iso(r["valid_from"]),
                "valid_to_iso": minute_to_iso(r["valid_to"]),
            }
            for r in rows
        ],
    }


@app.patch("/api/certificates/{cert_id}")
def patch_certificate(cert_id: int, body: CertificatePatch) -> dict:
    valid_from = parse_utc_minute(body.valid_from) if body.valid_from is not None else None
    valid_to = parse_utc_minute(body.valid_to) if body.valid_to is not None else None
    return services.update_certificate(
        cert_id, valid_from, valid_to, body.offset_c
    )


@app.delete("/api/certificates/{cert_id}")
def remove_certificate(cert_id: int) -> dict:
    return services.delete_certificate(cert_id)


# --------------------------------------------------------------------------- #
# Publications
# --------------------------------------------------------------------------- #
@app.post("/api/publications")
def publish(body: PublishIn) -> dict:
    return services.publish_day(body.probe_id, parse_day(body.day))


@app.post("/api/publications/recompute-all")
def recompute_all(
    probe_id: Optional[str] = None, day: Optional[str] = None
) -> dict:
    day_idx = parse_day(day) if day is not None else None
    return services.publish_many(probe_id, day_idx)


@app.get("/api/publications")
def query_publications(
    probe_id: Optional[str] = None,
    day: Optional[str] = None,
    include_history: bool = False,
) -> dict:
    """List the current publication of each (probe, day).

    Defaults to the latest row per pair (serving or stale — a stale row is
    still the queryable old version until a successful recompute replaces it).
    With ``include_history=true`` every version row is returned.
    """
    conn = db.connect()
    try:
        clauses, params = [], []
        if probe_id is not None:
            clauses.append("probe_id = ?")
            params.append(probe_id)
        if day is not None:
            clauses.append("day = ?")
            params.append(parse_day(day))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        if include_history:
            rows = conn.execute(
                f"SELECT * FROM publications {where} "
                "ORDER BY probe_id, day, id",
                params,
            ).fetchall()
            items = [_publication_item(r) for r in rows]
        else:
            # Latest row per (probe, day): its status is serving or stale.
            rows = conn.execute(
                f"SELECT p.* FROM publications p JOIN ("
                "SELECT probe_id AS pid, day AS d, MAX(id) AS max_id "
                f"FROM publications {where} GROUP BY probe_id, day"
                ") g ON p.id = g.max_id ORDER BY p.probe_id, p.day",
                params,
            ).fetchall()
            items = [_publication_item(r) for r in rows]

        return {"count": len(items), "publications": items}
    finally:
        conn.close()


def _publication_item(row) -> dict:
    payload = json.loads(row["payload"])
    return {
        "publication_id": row["id"],
        "probe_id": row["probe_id"],
        "day": row["day"],
        "day_utc": day_to_iso(row["day"]),
        "data_version": row["data_version"],
        "status": row["status"],
        "created_at": row["created_at"],
        "payload": payload,
    }


@app.get("/api/publications/{probe_id}/{day}")
def get_publication(probe_id: str, day: str, include_history: bool = False) -> dict:
    result = query_publications(probe_id, day, include_history)
    if not result["publications"]:
        return JSONResponse(
            status_code=404,
            content={"error": f"探头 {probe_id} 在 {day} 没有已发布版本"},
        )
    return result


# --------------------------------------------------------------------------- #
# Static web UI
# --------------------------------------------------------------------------- #
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/static/index.html")
