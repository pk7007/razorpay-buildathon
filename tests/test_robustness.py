"""Adversarial input. Every case here was found by trying to break the running
system, and each one is kept so it cannot come back."""
from __future__ import annotations

import io
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from finance_controller.pipeline import run_rows
from finance_controller.store import Store

ALL_SOURCES = ("payment", "settlement", "bank", "ledger", "refund", "chargeback")


@pytest.fixture(scope="module")
def client():
    os.environ["RECON_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "robust.db")
    from finance_controller import api
    from finance_controller.api import app

    with TestClient(app) as c:
        api._hits.clear()
        yield c


# --------------------------------------------------------------------- engine


def test_duplicate_entry_ids_do_not_break_conservation():
    """Re-exported statements repeat ids constantly. Two rows sharing one used to
    collapse into a single entry and take the whole run down with an assertion."""
    rows = {
        k: [{"id": "same", "amount": "1.00", "created_at": "2026-07-01",
             "payment_id": "same"}] * 3
        for k in ALL_SOURCES
    }
    r = run_rows(rows, dataset="dup", check_replay=False)
    assert r.metrics.total_entries == 18
    ids = [e.id for e in r.entries]
    assert len(ids) == len(set(ids)), "ids must be unique within a run"


@pytest.mark.parametrize("rows", [
    {},
    dict.fromkeys(ALL_SOURCES, []),
    {"payment": [{"id": "p", "amount": "0", "created_at": "2026-07-01"}]},
    {"payment": [{"id": "p", "amount": "-500.00", "created_at": "2026-07-01"}]},
    {"payment": [{"id": "p", "amount": "999999999999", "created_at": "2026-07-01"}]},
    {"payment": [{"id": "pay_日本_🎉", "amount": "1.00", "created_at": "2026-07-01"}]},
    {"payment": [{"id": None, "amount": None, "created_at": None}]},
    {"payment": [{"id": "p", "amount": "1.00", "created_at": "2099-12-31"}]},
    {"payment": [{"id": "p", "amount": "1.00", "created_at": "garbage"}]},
    {"refund": [{"id": "r1", "amount": "1.00", "created_at": "2026-07-01",
                 "payment_id": "r1"}]},
    {"chargeback": [{"id": "cb", "amount": "1.00", "created_at": "2026-07-01",
                     "payment_id": "x", "status": "???"}]},
])
def test_hostile_batches_never_crash(rows):
    r = run_rows(rows, dataset="hostile", check_replay=False)
    matched = [i for g in r.groups for i in g.entry_ids]
    exc = [e.entry_id for e in r.exceptions]
    assert sorted(matched + exc) == sorted(e.id for e in r.entries)


def test_a_refund_is_never_netted_across_currencies():
    r = run_rows({
        "payment": [{"id": "p", "amount": "100.00", "created_at": "2026-07-01",
                     "order_id": "O1", "currency": "USD"}],
        "refund": [{"id": "r", "amount": "50.00", "created_at": "2026-07-02",
                    "payment_id": "O1", "currency": "INR"}],
    }, dataset="mc", check_replay=False)
    assert r.groups == [], "a USD payment must not be reduced by an INR refund"


def test_refunds_exceeding_the_payment_are_survivable():
    r = run_rows({
        "payment": [{"id": "p", "amount": "100.00", "created_at": "2026-07-01",
                     "order_id": "O1"}],
        "refund": [{"id": "r", "amount": "999.00", "created_at": "2026-07-02",
                    "payment_id": "O1"}],
    }, dataset="over", check_replay=False)
    assert r.metrics.total_entries == 2
    assert any("exceed" in a.rationale for a in r.audit)


# ---------------------------------------------------------------------- store


def test_store_survives_hostile_values():
    st = Store(":memory:")
    from finance_controller import scenarios
    st.record_run(run_rows(scenarios.combined(), dataset="s", check_replay=False), "d")
    eid = st.list_exceptions()["items"][0]["id"]

    st.add_note(eid, "x" * 20_000)
    st.add_note(eid, "日本語 🎉 <script>alert(1)</script>")
    assert st.assign(eid, "प्रवीण")["assignee"] == "प्रवीण"

    # injection attempts must return nothing, not execute
    assert st.list_exceptions(category="'; DROP TABLE exceptions; --")["total"] == 0
    assert st.list_exceptions()["total"] > 0, "table was dropped"
    assert st.list_exceptions(offset=99_999)["items"] == []
    st.close()


# ------------------------------------------------------------------------ api


@pytest.mark.parametrize("path", [
    "/api/exceptions?q=' OR 1=1--",
    "/api/exceptions?sort=1;DROP TABLE exceptions--",
    "/api/exceptions?category=x' OR '1'='1",
    "/api/exceptions?limit=-5",
    "/api/exceptions?offset=-99",
])
def test_hostile_query_strings_are_handled(client, path):
    assert client.get(path).status_code == 200
    assert client.get("/api/exceptions").status_code == 200   # still alive


def test_dataset_path_traversal_is_refused(client):
    assert client.post("/api/reconcile", json={"dataset": "../../etc/passwd"}).status_code == 404


def test_limit_is_clamped(client):
    assert client.get("/api/exceptions?limit=999999").json()["limit"] == 500


@pytest.mark.parametrize("payload,expected", [
    ("hello", 422),
    ({}, 422),
])
def test_bad_patch_payloads(client, payload, expected):
    assert client.patch("/api/exceptions/nope", json=payload).status_code == expected


def test_note_must_be_a_string(client):
    assert client.post("/api/exceptions/x/notes", json={"body": 123}).status_code == 422


def test_ingest_rejects_an_unknown_source(client):
    r = client.post("/api/ingest/preview?source=evil",
                    files={"file": ("a.csv", io.BytesIO(b"a,b\n1,2"), "text/csv")})
    assert r.status_code == 422


def test_ingest_handles_bom_and_crlf(client):
    """Excel writes both. They must not become part of the first column name."""
    body = "﻿id,amount,date\r\np1,1.00,2026-07-01\r\n".encode()
    r = client.post("/api/ingest/preview?source=payment",
                    files={"file": ("a.csv", io.BytesIO(body), "text/csv")})
    assert r.status_code == 200 and r.json()["usable"]


def test_upload_of_a_non_csv_is_refused_cleanly(client):
    r = client.post("/api/reconcile/upload",
                    files={"payments": ("x.csv", io.BytesIO(b"<script>alert(1)</script>"),
                                        "text/csv")})
    assert r.status_code == 422


def test_a_csv_formula_is_data_not_an_instruction(client):
    """=cmd|... is an Excel injection payload. It must be treated as a value."""
    body = b"id,amount,date\n=cmd|' /C calc'!A0,1.00,2026-07-01"
    r = client.post("/api/ingest/preview?source=payment",
                    files={"file": ("a.csv", io.BytesIO(body), "text/csv")})
    assert r.status_code == 200
