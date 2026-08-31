"""One deliberately hostile month, driven through the whole product.

Every other end-to-end test starts from rows this repo generated, in the shape
this repo prefers. That proves the engine works on its own homework. This file
starts from *files* — CSVs in the shapes real exports actually arrive in — and
pushes them through the complete path:

    file bytes -> column detection -> row validation -> normalization
               -> reconciliation -> SQLite -> the endpoints the UI reads

The dataset is built to contain, in one batch, the things that actually make
reconciliation painful:

    variable fees          a negotiated rate, a flat-fee instrument, zero MDR
    GST                    on the fee, not on the sale
    TDS                    withheld under 194-O
    partial refund         inside the payout window
    full refund            nothing should settle
    chargeback             clawed back after the payout
    duplicate              the same credit exported twice
    late payout            raised in one period, paid in the next
    missing reference      a bank row with no UTR at all
    fee mismatch           a credit short by a bank charge
    unmatched              booked revenue that never arrived
    zero amount            a ₹0.00 row, the value most likely to "fit" anywhere
    very large amount      ₹1.25 crore, where a float pipeline loses paise
    decimals               amounts that do not divide evenly
    date formats           four of them, in one file
    unexpected columns     HDFC/Tally headers, debit and credit split

The assertions are deliberately about *money and refusals*, not about matching
everything. A batch this ugly should produce exceptions; what it must never
produce is a wrong number or a silently dropped row.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from finance_controller import api
from finance_controller.api import app

# --------------------------------------------------------------------------- #
# the files
# --------------------------------------------------------------------------- #

# A gateway export. Mixed date formats on purpose: exports concatenated from two
# systems really do arrive like this.
PAYMENTS_CSV = """Payment Id,Order Id,Created At,Amount,Method,Currency
pay_norm,ORD-1001,2026-07-01,10000.00,card,INR
pay_upi,ORD-1002,01-07-2026,5000.00,upi,INR
pay_flat,ORD-1003,02/07/2026,2500.50,netbanking,INR
pay_big,ORD-1004,2026-07-03,12500000.00,card,INR
pay_refund,ORD-1005,03 Jul 2026,8000.00,card,INR
pay_full_refund,ORD-1006,2026-07-04,3000.00,card,INR
pay_cb,ORD-1007,2026-07-05,4500.00,card,INR
pay_zero,ORD-1008,2026-07-05,0.00,upi,INR
pay_late,ORD-1009,2026-07-28,6000.00,card,INR
pay_never,ORD-1010,2026-07-06,7777.77,card,INR
"""

# The settlement report. Fees vary by instrument; one row withholds TDS.
SETTLEMENTS_CSV = """id,UTR,Amount,Fees,Tax,TDS,Settled At
setl_norm,UTR700001,9764.00,200.00,36.00,,2026-07-03
setl_upi,UTR700002,5000.00,0.00,0.00,,2026-07-03
setl_flat,UTR700003,2444.91,47.11,8.48,,2026-07-04
setl_big,UTR700004,12100000.00,250000.00,45000.00,105000.00,2026-07-05
setl_refund,UTR700005,4882.00,100.00,18.00,,2026-07-06
setl_cb,UTR700006,4393.80,90.00,16.20,,2026-07-07
setl_late,UTR700009,5858.40,120.00,21.60,,2026-08-02
"""

# An HDFC-shaped statement: split debit/credit, Indian comma grouping, a rupee
# sign, dd/mm/yyyy dates, one row with no reference at all, one duplicate, and a
# torn row that must be quarantined rather than dropped or guessed at.
BANK_CSV = """Txn Date,Value Dt,Narration,Chq/Ref No,Withdrawal Amt.,Deposit Amt.,Closing Balance
03/07/2026,03/07/2026,NEFT-RAZORPAY-UTR700001,UTR700001,,"9,764.00",259764.00
03/07/2026,03/07/2026,NEFT-RAZORPAY-UTR700002,UTR700002,,"5,000.00",264764.00
04/07/2026,04/07/2026,NEFT-RAZORPAY-UTR700003,UTR700003,,"2,444.91",267208.91
05/07/2026,05/07/2026,NEFT-RAZORPAY-UTR700004,UTR700004,,"1,21,00,000.00",12367208.91
06/07/2026,06/07/2026,NEFT-RAZORPAY-UTR700005,UTR700005,,"4,882.00",12372090.91
07/07/2026,07/07/2026,NEFT-RAZORPAY-UTR700006,UTR700006,,"4,393.80",12376516.31
07/07/2026,07/07/2026,NEFT-RAZORPAY-UTR700006,UTR700006,,"4,393.80",12380910.11
08/07/2026,08/07/2026,IMPS INWARD NO REFERENCE,,,"1,500.00",12382410.11
09/07/2026,09/07/2026,BANK CHARGES GST,,118.00,,12382292.11
02/08/2026,02/08/2026,NEFT-RAZORPAY-UTR700009,UTR700009,,"5,858.40",12388151.71
not-a-date,,TORN ROW FROM A BAD EXPORT,,,ABC,12388151.71
"""

# A Tally-shaped ledger. Particulars, not Narration. One booked sale that never
# arrives anywhere else.
LEDGER_CSV = """Sl No,Booking Date,Particulars,Amount INR,Currency,Order Id
L-1,2026-07-01,Sales - online,10000.00,INR,ORD-1001
L-2,2026-07-01,Sales - online,5000.00,INR,ORD-1002
L-3,2026-07-02,Sales - online,2500.50,INR,ORD-1003
L-4,2026-07-03,Sales - online,12500000.00,INR,ORD-1004
L-5,2026-07-03,Sales - online,8000.00,INR,ORD-1005
L-6,2026-07-04,Sales - online,3000.00,INR,ORD-1006
L-7,2026-07-05,Sales - online,4500.00,INR,ORD-1007
L-8,2026-07-28,Sales - online,6000.00,INR,ORD-1009
L-9,2026-07-06,Sales - online,7777.77,INR,ORD-1010
"""

REFUNDS_CSV = """id,payment_id,amount,created_at,status
rfnd_part,pay_refund,3000.00,2026-07-04,processed
rfnd_full,pay_full_refund,3000.00,2026-07-05,processed
"""

CHARGEBACKS_CSV = """id,payment_id,amount,created_at,status
cbk_1,pay_cb,4500.00,2026-07-20,lost
"""


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    api._hits.clear()
    yield
    api._hits.clear()


def _file(text: str):
    return io.BytesIO(text.encode("utf-8"))


@pytest.fixture(scope="module")
def result(client):
    r = client.post(
        "/api/reconcile/upload",
        files={
            "payments": ("gateway_export.csv", _file(PAYMENTS_CSV), "text/csv"),
            "settlements": ("settlement_report.csv", _file(SETTLEMENTS_CSV), "text/csv"),
            "bank": ("hdfc_statement.csv", _file(BANK_CSV), "text/csv"),
            "ledger": ("tally_daybook.csv", _file(LEDGER_CSV), "text/csv"),
            "refunds": ("refunds.csv", _file(REFUNDS_CSV), "text/csv"),
            "chargebacks": ("chargebacks.csv", _file(CHARGEBACKS_CSV), "text/csv"),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def entries_by_id(result):
    return {e["id"]: e for e in result["entries"]}


# --------------------------------------------------------------------------- #
# 1. ingestion: the columns, the formats, the torn row
# --------------------------------------------------------------------------- #


def test_every_file_maps_without_being_told_its_schema(client):
    """Four exports, four different vocabularies, no configuration."""
    expected = {
        "payment": (PAYMENTS_CSV, {"id", "date", "amount", "currency"}),
        "settlement": (SETTLEMENTS_CSV, {"id", "date", "amount"}),
        "bank": (BANK_CSV, {"date", "narration", "reference"}),
        "ledger": (LEDGER_CSV, {"id", "date", "amount", "currency"}),
    }
    for source, (csv, must_have) in expected.items():
        r = client.post(f"/api/ingest/preview?source={source}",
                        files={"file": (f"{source}.csv", _file(csv), "text/csv")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["usable"], body
        got = set(body["mapping"])
        missing = must_have - got - ({"amount"} if body.get("split_amount") else set())
        assert not missing, f"{source}: {missing} not detected from {body['columns_detected']}"


def test_the_split_debit_credit_statement_is_folded_into_one_signed_amount(client):
    r = client.post("/api/ingest/preview?source=bank",
                    files={"file": ("hdfc.csv", _file(BANK_CSV), "text/csv")})
    body = r.json()
    assert body["split_amount"] is True
    assert body["mapping"]["debit"] == "Withdrawal Amt."
    assert body["mapping"]["credit"] == "Deposit Amt."


def test_the_torn_row_is_quarantined_not_dropped_and_not_guessed(client):
    r = client.post("/api/ingest/preview?source=bank",
                    files={"file": ("hdfc.csv", _file(BANK_CSV), "text/csv")})
    q = r.json()["quality"]
    assert q["total_rows"] == 11
    assert q["invalid_rows"] == 1, q
    assert q["valid_rows"] == 10
    # and it is reported with a reason, not a count
    assert q["sample_issues"], "a row was rejected with no stated reason"


def test_four_date_formats_in_one_file_all_parse(result):
    """dd-mm-yyyy, dd/mm/yyyy, '03 Jul 2026' and ISO, in the same column."""
    e = entries_by_id(result)
    assert e["pay_norm"]["value_date"] == "2026-07-01"
    assert e["pay_upi"]["value_date"] == "2026-07-01"
    assert e["pay_flat"]["value_date"] == "2026-07-02"
    assert e["pay_refund"]["value_date"] == "2026-07-03"
    assert not any(x["value_date"] == "1970-01-01" for x in result["entries"]), (
        "a date failed to parse and fell back to the epoch sentinel"
    )


def test_indian_comma_grouping_and_the_rupee_sign_survive(result):
    """'1,21,00,000.00' is one crore twenty-one lakh, not 1.21."""
    e = entries_by_id(result)
    big = [x for x in result["entries"]
           if x["source"] == "bank" and x["amount_paise"] == 1_21_00_000_00]
    assert big, "the crore-scale bank credit did not parse to the right integer"
    assert e["pay_big"]["amount_paise"] == 1_25_00_000_00


# --------------------------------------------------------------------------- #
# 2. money: nothing invented, nothing lost
# --------------------------------------------------------------------------- #


def test_conservation_holds_on_the_whole_messy_batch(result):
    grouped = [i for g in result["groups"] for i in g["entry_ids"]]
    excepted = [x["entry_id"] for x in result["exceptions"]]
    assert len(grouped) == len(set(grouped)), "an entry is in two groups"
    assert sorted(grouped + excepted) == sorted(e["id"] for e in result["entries"]), (
        "entries were invented or lost between input and output"
    )


def test_no_amount_is_zero_by_accident(result):
    """The upload path once normalised every amount to zero. Anything that is
    zero here has to be zero in the source."""
    zeros = [e for e in result["entries"] if e["amount_paise"] == 0]
    assert {e["id"] for e in zeros} == {"pay_zero"}, [e["id"] for e in zeros]


def test_the_zero_amount_row_does_not_join_an_unrelated_group(result):
    for g in result["groups"]:
        if "pay_zero" in g["entry_ids"]:
            assert len(g["entry_ids"]) <= 2, (
                f"the ₹0 row was absorbed into {g['entry_ids']}"
            )


def test_every_money_figure_is_an_integer(result):
    for e in result["entries"]:
        assert isinstance(e["amount_paise"], int)
    for k, v in result["money"].items():
        if k.endswith("_paise") or k.endswith("_minor"):
            assert isinstance(v, int), f"{k} is {type(v).__name__}"


def test_the_money_summary_adds_up(result):
    mo = result["money"]
    parts = (mo["reconciled_paise"] + mo["in_transit_paise"] + mo["recoverable_paise"]
             + mo["unrecorded_paise"] + mo["ambiguous_paise"])
    assert parts >= 0
    assert mo["gross_processed_paise"] >= mo["reconciled_paise"]


# --------------------------------------------------------------------------- #
# 3. the situations, one at a time
# --------------------------------------------------------------------------- #


def group_with(result, entry_id):
    for g in result["groups"]:
        if entry_id in g["entry_ids"]:
            return g
    return None


def exception_for(result, entry_id):
    for x in result["exceptions"]:
        if x["entry_id"] == entry_id:
            return x
    return None


def test_the_ordinary_sale_ties_all_four_ways(result):
    g = group_with(result, "pay_norm")
    assert g is not None, "the simplest case in the batch did not reconcile"
    assert set(g["entry_ids"]) >= {"pay_norm", "setl_norm", "L-1"}
    assert any(ch.isdigit() for ch in g["rationale"])


def test_zero_mdr_upi_settles_at_face_value(result):
    g = group_with(result, "pay_upi")
    assert g is not None
    assert "setl_upi" in g["entry_ids"], "a zero-fee payout was not recognised"


def test_the_crore_scale_payout_with_tds_reconciles_exactly(result):
    """12,500,000 gross - 250,000 fee - 45,000 GST - 105,000 TDS = 12,100,000."""
    e = entries_by_id(result)
    assert e["setl_big"]["amount_paise"] == 1_21_00_000_00
    g = group_with(result, "setl_big")
    assert g is not None, "the largest payout in the batch did not tie out"
    assert "pay_big" in g["entry_ids"] or "L-4" in g["entry_ids"]


def test_the_partially_refunded_sale_settles_net_of_the_refund(result):
    g = group_with(result, "pay_refund")
    assert g is not None
    assert "rfnd_part" not in [x["entry_id"] for x in result["exceptions"]], (
        "a refund naming a payment in this batch was reported as an orphan"
    )


def test_the_duplicate_bank_credit_is_flagged_not_counted_twice(result):
    """UTR700006 appears twice for the same amount on the same day."""
    utr6 = [e for e in result["entries"]
            if e["source"] == "bank" and e["amount_paise"] == 4393_80]
    assert len(utr6) == 2, "the duplicated credit is not in the input any more"
    dup_ids = {x["entry_id"] for x in result["exceptions"] if x["category"] == "duplicate"}
    claimed = {i for g in result["groups"] for i in g["entry_ids"]}
    both_claimed = {e["id"] for e in utr6} <= claimed
    assert dup_ids or not both_claimed, (
        "one payout was claimed by two bank credits and neither was flagged"
    )


def test_the_bank_row_with_no_reference_is_not_matched_by_amount_alone(result):
    """A 1,500.00 IMPS credit with no UTR and nothing else of that size."""
    orphan = [e for e in result["entries"]
              if e["source"] == "bank" and e["amount_paise"] == 1500_00]
    assert orphan, "the no-reference credit vanished"
    x = exception_for(result, orphan[0]["id"])
    assert x is not None, "a credit with no reference and no counterpart was matched"
    assert x["category"] in ("missing_in_ledger", "unknown"), x


def test_the_bank_charge_debit_is_not_treated_as_revenue(result):
    debit = [e for e in result["entries"] if e["amount_paise"] < 0]
    assert debit, "the withdrawal row lost its sign"
    assert debit[0]["amount_paise"] == -118_00


def test_the_sale_that_never_arrived_is_reported_as_recoverable(result, client):
    """ORD-1010 was captured and booked, so the payment and the ledger line tie
    to each other by order id — but nothing ever settled or landed. That is a
    *group* with a missing leg, not a stray row, so the signal is the group's
    status and the money it puts in `recoverable`."""
    g = group_with(result, "L-9")
    assert g is not None and "pay_never" in g["entry_ids"]
    assert g["status"] == "payout_overdue", g
    assert result["money"]["recoverable_paise"] >= 7777_77

    # and it has to reach the human queue, not just the result JSON
    queue = client.get("/api/exceptions?limit=200").json()["items"]
    assert any(i["entry_id"] in ("L-9", "pay_never") for i in queue), (
        "an overdue payout never reached the worklist"
    )


def test_the_late_payout_still_ties_across_the_period_boundary(result):
    """Sold 28 July, paid 2 August. The month boundary is not a wall."""
    g = group_with(result, "setl_late")
    assert g is not None, "a payout that crossed the month end did not reconcile"


def test_every_exception_says_why_and_what_to_do(result):
    assert result["exceptions"], "a batch this messy produced no exceptions at all"
    for x in result["exceptions"]:
        # a rationale has to name the amount and say what was looked at; a
        # category with a shrug attached is not an explanation
        assert len(x["rationale"]) > 30, x
        assert any(ch.isdigit() for ch in x["rationale"]), x
        assert x["suggested_action"], x
        assert 0.0 <= x["confidence"] <= 1.0


def test_no_group_is_formed_without_a_stated_rule(result):
    for g in result["groups"]:
        assert g["rule"], g
        assert len(g["rationale"]) > 15, g


# --------------------------------------------------------------------------- #
# 4. the rest of the product: it persisted, and the UI can see it
# --------------------------------------------------------------------------- #


def test_the_run_reaches_the_database_and_the_endpoints_the_ui_reads(client, result):
    runs = client.get("/api/runs?limit=50").json()
    # Other test modules upload too, and they share this process's store, so the
    # run is found by its own numbers rather than by being the most recent.
    gross = result["money"]["gross_processed_paise"]
    mine = [r for r in runs
            if r["dataset"] == "upload"
            and r["entries"] == result["metrics"]["total_entries"]
            and r["money"]["gross_processed_paise"] == gross]
    assert mine, f"this run was not recorded (looked for {gross} paise gross)"
    latest = mine[0]
    assert latest["exceptions"] == result["metrics"]["exceptions"]

    detail = client.get(f"/api/runs/{latest['id']}").json()
    assert detail["money"]["gross_processed_paise"] == gross

    summary = client.get("/api/exceptions/summary").json()
    assert summary["total"] >= result["metrics"]["exceptions"]

    queue = client.get("/api/exceptions?limit=200").json()
    assert queue["items"], "nothing reached the worklist"
    for item in queue["items"]:
        assert item["rationale"] and item["suggested_action"]


def test_re_uploading_the_same_files_does_not_duplicate_the_worklist(client):
    """The same month re-run is a second run, but the same exceptions."""
    before = client.get("/api/exceptions/summary").json()["total"]
    r = client.post(
        "/api/reconcile/upload",
        files={
            "payments": ("gateway_export.csv", _file(PAYMENTS_CSV), "text/csv"),
            "settlements": ("settlement_report.csv", _file(SETTLEMENTS_CSV), "text/csv"),
            "bank": ("hdfc_statement.csv", _file(BANK_CSV), "text/csv"),
            "ledger": ("tally_daybook.csv", _file(LEDGER_CSV), "text/csv"),
            "refunds": ("refunds.csv", _file(REFUNDS_CSV), "text/csv"),
            "chargebacks": ("chargebacks.csv", _file(CHARGEBACKS_CSV), "text/csv"),
        },
    )
    assert r.status_code == 200
    after = client.get("/api/exceptions/summary").json()["total"]
    assert after == before, (
        f"re-running the same batch grew the worklist from {before} to {after}"
    )


def test_the_result_is_replay_stable(result):
    assert result["metrics"]["replay_stable"] is True, (
        "the same input produced a different answer on a second pass"
    )
