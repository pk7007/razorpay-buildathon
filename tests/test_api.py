"""API contract tests via FastAPI's TestClient."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finance_controller import api
from finance_controller.api import app

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "datasets" / "realistic"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # runs the lifespan warmup
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter is process-global; a shared bucket would make test order matter."""
    api._hits.clear()
    yield
    api._hits.clear()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["resolver"] in ("llm", "heuristic")


def test_datasets_listed_in_difficulty_order(client):
    names = [d["name"] for d in client.get("/api/datasets").json()]
    assert names[:3] == ["clean", "realistic", "messy"]


def test_reconcile_bundled(client):
    r = client.post("/api/reconcile", json={"dataset": "realistic"})
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["precision"] == 1.0
    assert body["metrics"]["recall"] == 1.0
    assert len(body["groups"]) > 0
    assert body["resolver_mode"] == "heuristic"
    # conservation across the wire
    matched = {i for g in body["groups"] for i in g["entry_ids"]}
    exc = {e["entry_id"] for e in body["exceptions"]}
    assert len(matched) + len(exc) == len(body["entries"])


def test_reconcile_unknown_dataset(client):
    assert client.post("/api/reconcile", json={"dataset": "nope"}).status_code == 404


def test_upload_roundtrip(client):
    payload = {}
    for field, fname in [
        ("payments", "payments.csv"), ("settlements", "settlements.csv"),
        ("bank", "bank.csv"), ("ledger", "ledger.csv"),
    ]:
        payload[field] = (fname, io.BytesIO((SAMPLE / fname).read_bytes()), "text/csv")
    r = client.post("/api/reconcile/upload", files=payload)
    assert r.status_code == 200
    # same row count as the bundled dataset (the upload path is file-source agnostic)
    bundled = client.post("/api/reconcile", json={"dataset": "realistic"}).json()
    assert r.json()["metrics"]["total_entries"] == bundled["metrics"]["total_entries"]
    assert len(r.json()["groups"]) == len(bundled["groups"])


def test_upload_requires_a_file(client):
    assert client.post("/api/reconcile/upload").status_code == 400


def test_upload_rejects_unparseable(client):
    r = client.post(
        "/api/reconcile/upload",
        files={"payments": ("x.json", io.BytesIO(b"{not json"), "application/json")},
    )
    assert r.status_code == 422


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "AI Finance Controller" in r.text


def test_response_does_not_echo_raw_rows(client):
    body = client.post("/api/reconcile", json={"dataset": "clean"}).json()
    assert body["entries"] and all("raw" not in e for e in body["entries"])


def test_upload_row_cap(client, monkeypatch):
    """Rows are capped. Patched low so the test exercises the guard rather than
    spending a minute actually reconciling 100k rows."""
    monkeypatch.setattr(api, "_MAX_ROWS", 10)
    big = "id,amount,created_at,order_id\n" + "\n".join(
        f"p{i},10.00,2026-07-01,O{i}" for i in range(50)
    )
    r = client.post(
        "/api/reconcile/upload",
        files={"payments": ("p.csv", io.BytesIO(big.encode()), "text/csv")},
    )
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"]


def test_upload_rejects_empty_file(client):
    r = client.post(
        "/api/reconcile/upload",
        files={"payments": ("p.csv", io.BytesIO(b""), "text/csv")},
    )
    assert r.status_code == 422


def test_error_bodies_never_echo_the_filename(client):
    """A filename is client-controlled text; reflecting it verbatim is an XSS
    vector for anything that renders the error."""
    nasty = "<img src=x onerror=alert(1)>.csv"
    r = client.post(
        "/api/reconcile/upload",
        files={"payments": (nasty, io.BytesIO(b"{bad"), "application/json")},
    )
    assert r.status_code == 422
    body = r.text
    assert "<img" not in body and "onerror" not in body


def test_responses_carry_request_id_and_security_headers(client):
    r = client.get("/api/health")
    assert r.headers.get("X-Request-ID")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_rate_limit_kicks_in(client, monkeypatch):
    monkeypatch.setattr(api, "_hits", {})
    monkeypatch.setattr(api, "_RATE_LIMIT", 3)
    codes = [
        client.post("/api/reconcile", json={"dataset": "nope"}).status_code
        for _ in range(5)
    ]
    assert 429 in codes, codes
    monkeypatch.setattr(api, "_hits", {})


def test_reconcile_rejects_non_string_dataset(client, monkeypatch):
    monkeypatch.setattr(api, "_hits", {})
    for bad in ({"dataset": 123}, {"dataset": None}, {}, {"dataset": ["clean"]}):
        assert client.post("/api/reconcile", json=bad).status_code == 404
