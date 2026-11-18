import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    db.configure(str(tmp_path / "test.db"))
    db.init_db()
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def service_db(tmp_path):
    """Direct service-layer handle with an isolated database."""
    db.configure(str(tmp_path / "svc.db"))
    db.init_db()
    yield db


CSV_HEADER = "probe_id,minute_utc,temperature_c\n"


def import_csv(client, text):
    return client.post(
        "/api/readings/import",
        files={"file": ("r.csv", text.encode("utf-8"), "text/csv")},
    )


def add_cert(client, probe, start, end, offset):
    return client.post(
        "/api/certificates",
        json={
            "probe_id": probe,
            "valid_from": start,
            "valid_to": end,
            "offset_c": offset,
        },
    )
