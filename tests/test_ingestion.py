"""Column mapping, data quality, and the Razorpay ingestion path.

The upload path used to work only on files we generated ourselves. These tests
use the header layouts real Indian bank statements actually arrive with.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from finance_controller import api
from finance_controller.api import app
from finance_controller.mapping import apply_mapping, detect
from finance_controller.pipeline import run_rows
from finance_controller.quality import combined_summary, validate
from finance_controller.razorpay_source import (
    RazorpayUnavailable,
    fetch_live,
    fixture_batch,
    load,
)

# header rows as these banks actually export them
BANK_HEADERS = {
    "hdfc": ["Date", "Narration", "Chq/Ref No", "Withdrawal Amt.", "Deposit Amt.",
             "Closing Balance"],
    "icici": ["S No.", "Value Date", "Transaction Date", "Cheque Number",
              "Transaction Remarks", "Withdrawal Amount (INR )",
              "Deposit Amount (INR )", "Balance (INR )"],
    "sbi": ["Txn Date", "Value Date", "Description", "Ref No./Cheque No.",
            "        Debit", "        Credit", "       Balance"],
    "axis": ["Tran Date", "CHQNO", "PARTICULARS", "DR", "CR", "BAL", "SOL"],
    "kotak": ["Sl. No.", "Transaction Date", "Value Date", "Description",
              "Amount", "Dr / Cr", "Balance"],
}


@pytest.mark.parametrize("bank", list(BANK_HEADERS))
def test_real_bank_headers_are_usable(bank):
    cm = detect("bank", BANK_HEADERS[bank])
    assert cm.ok, f"{bank} not usable: {cm.explain()}"
    assert "date" in cm.mapping
    assert "amount" in cm.mapping or cm.split_amount


def test_a_date_column_is_never_mapped_to_amount():
    """'Value Date' once scored as an amount because 'value' was an alias --
    which would have silently reconciled against a date."""
    for headers in BANK_HEADERS.values():
        cm = detect("bank", headers)
        amount_col = cm.mapping.get("amount", "")
        assert "date" not in amount_col.lower()


def test_value_date_is_preferred_over_transaction_date():
    """Both appear on most statements; the value date is when money moved."""
    cm = detect("bank", ["Txn Date", "Value Date", "Description", "Amount"])
    assert cm.mapping["date"] == "Value Date"


def test_split_debit_credit_becomes_one_signed_amount():
    cm = detect("bank", BANK_HEADERS["hdfc"])
    rows = [
        {"Date": "01/07/2026", "Narration": "NEFT RAZORPAY UTR12345678",
         "Chq/Ref No": "UTR12345678", "Withdrawal Amt.": "",
         "Deposit Amt.": "9,764.00", "Closing Balance": "1,00,000.00"},
        {"Date": "02/07/2026", "Narration": "BANK CHARGES", "Chq/Ref No": "",
         "Withdrawal Amt.": "118.00", "Deposit Amt.": "", "Closing Balance": "99,882.00"},
    ]
    out = apply_mapping(cm, rows)
    assert out[0]["amount"] == 9764.0 and out[0]["type"] == "credit"
    assert out[1]["amount"] == -118.0 and out[1]["type"] == "debit"


def test_genuinely_ambiguous_columns_are_refused_not_guessed():
    cm = detect("bank", ["Date", "Amount", "Transaction Amount", "Narration"])
    assert not cm.ok
    assert "amount" in cm.ambiguous


def test_missing_required_columns_are_named_with_the_fix():
    cm = detect("payment", ["foo", "bar"])
    assert set(cm.missing_required) == {"amount", "date"}
    assert any("MISSING" in line for line in cm.explain())


def test_indian_number_formatting_parses():
    cm = detect("bank", ["Value Date", "Description", "Amount"])
    out = apply_mapping(cm, [{"Value Date": "01/07/2026", "Description": "x",
                              "Amount": "1,23,456.78"}])
    assert out[0]["amount"] == "1,23,456.78"    # normalize handles the commas


# ------------------------------------------------------------------ data quality


def test_bad_rows_are_quarantined_and_good_rows_survive():
    rows = [
        {"id": "p1", "amount": "100.00", "created_at": "2026-07-01"},
        {"id": "p2", "amount": "not-a-number", "created_at": "2026-07-01"},
        {"id": "p3", "amount": "50.00", "created_at": "banana"},
        {"id": "p4", "amount": "75.00", "created_at": "2026-07-02"},
    ]
    accepted, rep = validate("payment", rows)
    assert rep.total_rows == 4
    assert rep.valid_rows == 2 and rep.invalid_rows == 2
    assert {r["id"] for r in accepted} == {"p1", "p4"}
    assert not rep.ok


def test_duplicate_ids_are_detected():
    rows = [
        {"id": "p1", "amount": "100.00", "created_at": "2026-07-01"},
        {"id": "p1", "amount": "100.00", "created_at": "2026-07-01"},
    ]
    _, rep = validate("payment", rows)
    assert rep.duplicate_rows == 1
    assert any("duplicate id" in i.problem for i in rep.issues)


def test_empty_rows_are_counted_not_treated_as_errors():
    rows = [
        {"id": "p1", "amount": "100.00", "created_at": "2026-07-01"},
        {"id": "", "amount": "", "created_at": ""},
    ]
    accepted, rep = validate("payment", rows)
    assert rep.empty_rows == 1
    assert len(accepted) == 1


def test_implausible_amounts_are_rejected():
    _, rep = validate("payment", [
        {"id": "p1", "amount": "999999999999999", "created_at": "2026-07-01"}
    ])
    assert rep.invalid_rows == 1


def test_unsupported_currency_is_a_warning_not_a_rejection():
    accepted, rep = validate("payment", [
        {"id": "p1", "amount": "10.00", "created_at": "2026-07-01", "currency": "XYZ"}
    ])
    assert len(accepted) == 1
    assert any(i.severity == "warning" for i in rep.issues)


def test_a_deduction_that_names_no_payment_warns():
    _, rep = validate("refund", [
        {"id": "r1", "amount": "10.00", "created_at": "2026-07-01"}
    ])
    assert any("does not name the payment" in i.problem for i in rep.issues)


def test_validate_never_raises_on_hostile_input():
    for rows in ([{"id": None}], [[]], [None], [{"amount": {"nested": 1}}], []):
        accepted, rep = validate("payment", rows)   # type: ignore[arg-type]
        assert isinstance(accepted, list) and rep.total_rows == len(rows)


def test_combined_summary_rolls_up():
    reports = {}
    for src in ("payment", "bank"):
        _, rep = validate(src, [{"id": "x", "amount": "1.00", "date": "2026-07-01"}])
        reports[src] = rep
    out = combined_summary(reports)
    assert out["total_rows"] == 2 and out["accepted_pct"] == 1.0


# --------------------------------------------------------------------- razorpay


def test_fixtures_are_labelled_and_never_claim_to_be_live():
    b = fixture_batch()
    assert b.provenance == "fixture"
    assert "NOT pulled from Razorpay" in b.note


def test_live_pull_refuses_without_credentials_rather_than_faking():
    with pytest.raises(RazorpayUnavailable):
        fetch_live()


def test_load_falls_back_to_fixtures_and_says_so():
    b = load()
    assert b.provenance in ("fixture", "live_test")
    assert b.summary()["provenance"] == b.provenance


def test_razorpay_shaped_data_reconciles():
    """Epoch timestamps, integer paise, pay_/rfnd_ ids, refunds naming a
    payment by id -- the shape the real API returns."""
    b = fixture_batch()
    r = run_rows(b.as_rows(), dataset="razorpay-fixture", check_replay=False)
    assert r.metrics.auto_match_rate >= 0.85
    # the refund must attach to its payment, not become an orphan
    assert not any(e.category == "orphan_refund" for e in r.exceptions)


def test_uncaptured_payments_are_excluded():
    """An authorized-but-uncaptured payment is not money owed and must not be
    reported as a missing payout."""
    b = fixture_batch()
    assert all(p["status"] == "captured" for p in b.payments)


# --------------------------------------------------------------------------- #
# The upload endpoint must apply the mapping it previews
# --------------------------------------------------------------------------- #
#
# /api/ingest/preview used to detect columns, map them and report row quality,
# while /api/reconcile/upload fed the raw parsed rows straight to the engine.
# The preview therefore promised a mapping the run never applied: every amount
# normalized to zero, every date to 1970-01-01, and the engine then "matched"
# rows worth nothing at all with a heuristic confidence of 75%. Silent zeroes
# are the worst possible failure for a reconciliation tool, so this is pinned.

HDFC_CSV = (
    b"Txn Date,Value Dt,Narration,Chq/Ref No,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    b"2026-07-01,2026-07-01,NEFT-ACME-UTR8811,UTR8811,,49523.10,249523.10\n"
    b"2026-07-03,2026-07-03,NEFT-ACME-UTR8812,UTR8812,,18240.55,267763.65\n"
    b"2026-07-06,2026-07-06,BANK CHARGES GST,,118.00,,267645.65\n"
)

TALLY_CSV = (
    b"Sl No,Booking Date,Particulars,Amount INR,Currency,Order Id\n"
    b"L-1,2026-07-01,Sales - online orders,50000.00,INR,ORD9001\n"
    b"L-2,2026-07-03,Sales - online orders,18500.00,INR,ORD9002\n"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:                      # runs the lifespan warmup
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter is process-global; a shared bucket would make test order matter."""
    api._hits.clear()
    yield
    api._hits.clear()


def _upload(client, **files):
    return client.post(
        "/api/reconcile/upload",
        files={k: (f"{k}.csv", io.BytesIO(v), "text/csv") for k, v in files.items()},
    )


def test_upload_applies_the_mapping_the_preview_shows(client):
    r = _upload(client, bank=HDFC_CSV, ledger=TALLY_CSV)
    assert r.status_code == 200, r.text
    body = r.json()

    by_id = {e["id"]: e for e in body["entries"]}
    amounts = sorted(e["amount_paise"] for e in by_id.values() if e["source"] == "bank")
    assert amounts == [-11800, 1824055, 4952310], amounts
    assert all(e["value_date"] != "1970-01-01" for e in by_id.values())
    assert body["money"]["in_exception_paise"] > 0


def test_upload_never_matches_on_zeroed_amounts(client):
    """A group whose members are all worth nothing is not a match."""
    body = _upload(client, bank=HDFC_CSV, ledger=TALLY_CSV).json()
    for g in body["groups"]:
        assert g["amount_paise"] != 0, g


def test_upload_refuses_an_ambiguous_amount_column(client):
    ambiguous = (
        b"Date,Amount,Transaction Amount,Narration\n"
        b"2026-07-01,100.00,100.00,duplicate amount columns\n"
    )
    r = _upload(client, bank=ambiguous)
    assert r.status_code == 422
    assert "unambiguous" in r.json()["detail"].lower()


def test_upload_keeps_good_rows_and_drops_torn_ones(client):
    torn = HDFC_CSV + b"not-a-date,,TORN ROW,,,ABC,276555.65\n"
    r = _upload(client, bank=torn, ledger=TALLY_CSV)
    assert r.status_code == 200, r.text
    # the torn row is dropped; the three good bank rows and two ledger rows survive
    assert r.json()["metrics"]["total_entries"] == 5
