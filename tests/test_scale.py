"""Cost of a batch, and what happens when one is hostile.

A reconciliation engine that is fast on data it matches and quadratic on data it
does not is not fast — the slow path is exactly the one a bad month takes. These
tests pin the shape of the curve rather than a wall-clock number, so they stay
meaningful on a slower machine and still fail if the complexity regresses.

Two production bugs are pinned here:

* `_heuristic_resolve` compared every residual entry against every other one.
  6,000 unmatched rows took 3.5s and 20,000 took a minute, so a single uploaded
  month with a large unmatched tail could hang the server.
* the upload endpoint enforced its row cap *after* column detection, mapping and
  validation, and counted only surviving rows — so an oversized file paid the
  full O(rows x columns) cost before being rejected, and a file of mostly-invalid
  rows slipped under the cap entirely.
"""
from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from finance_controller import api
from finance_controller.api import app
from finance_controller.pipeline import run_rows


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    api._hits.clear()
    yield
    api._hits.clear()


def identical_rows(n: int) -> dict:
    """The pathological batch: every row the same amount on the same day.

    Nothing here can be matched on amount, because the amount identifies
    nothing. It is the worst case for any pairwise scorer and the easiest one
    for an attacker to construct.
    """
    return {
        "bank": [{"id": f"r{i}", "amount": "1.00", "date": "2026-07-01"} for i in range(n)],
        "payment": [], "settlement": [], "ledger": [], "refund": [], "chargeback": [],
    }


def elapsed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


# --------------------------------------------------------------------------- #
# 1. the shape of the curve
# --------------------------------------------------------------------------- #


def test_a_large_unmatched_batch_does_not_scale_quadratically():
    """Quadrupling the rows must not multiply the time by ~16.

    The threshold is deliberately loose (8x for a 4x increase) so that ordinary
    timing noise cannot fail it, while genuine O(n^2) — which would be ~16x —
    still does.
    """
    small = elapsed(lambda: run_rows(identical_rows(1_500), dataset="s", check_replay=False))
    large = elapsed(lambda: run_rows(identical_rows(6_000), dataset="l", check_replay=False))

    # a floor, so a fast machine measuring near zero cannot produce a wild ratio
    if small < 0.02:
        small = 0.02
    ratio = large / small
    assert ratio < 8.0, (
        f"4x the rows cost {ratio:.1f}x the time — that is quadratic, not linear. "
        f"{small:.3f}s for 1,500 rows, {large:.3f}s for 6,000."
    )


def test_a_hostile_batch_still_completes_in_seconds():
    """20,000 identical unmatched rows used to take about a minute."""
    took = elapsed(lambda: run_rows(identical_rows(20_000), dataset="hostile",
                                    check_replay=False))
    assert took < 20.0, f"20,000 unmatched rows took {took:.1f}s"


def test_nothing_is_lost_in_a_hostile_batch():
    """Speed must not come from dropping rows."""
    r = run_rows(identical_rows(5_000), dataset="hostile", check_replay=False)
    assert r.metrics.total_entries == 5_000
    grouped = [i for g in r.groups for i in g.entry_ids]
    excepted = [e.entry_id for e in r.exceptions]
    assert sorted(grouped + excepted) == sorted(e.id for e in r.entries)


def test_an_amount_shared_by_a_crowd_is_refused_rather_than_guessed():
    """When hundreds of entries share one amount, that amount identifies nothing.
    Picking a pair out of the crowd would be a guess, so none is picked."""
    rows = {
        "bank": [{"id": f"b{i}", "amount": "500.00", "date": "2026-07-01"} for i in range(200)],
        "ledger": [{"id": f"l{i}", "amount": "500.00", "date": "2026-07-01"} for i in range(200)],
        "payment": [], "settlement": [], "refund": [], "chargeback": [],
    }
    r = run_rows(rows, dataset="crowd", check_replay=False)
    assert not r.groups, (
        f"{len(r.groups)} pairs were matched out of 400 identical amounts — "
        f"each one is a coin toss"
    )
    assert r.metrics.exceptions == 400


def test_a_small_batch_of_distinct_amounts_still_matches():
    """The blocking must not have thrown away the matching along with the cost."""
    rows = {
        "settlement": [
            {"id": f"s{i}", "utr": f"UTR{i:05d}", "amount": f"{100 + i}.00",
             "settled_at": "2026-07-01"}
            for i in range(20)
        ],
        "bank": [
            {"id": f"b{i}", "amount": f"{100 + i}.00", "date": "2026-07-02",
             "narration": f"NEFT RAZORPAY UTR{i:05d}"}
            for i in range(20)
        ],
        "payment": [], "ledger": [], "refund": [], "chargeback": [],
    }
    r = run_rows(rows, dataset="ok", check_replay=False)
    assert len(r.groups) == 20, f"only {len(r.groups)} of 20 payouts tied out"


# --------------------------------------------------------------------------- #
# 2. the upload cap, and when it is applied
# --------------------------------------------------------------------------- #


def csv_rows(n: int, valid: bool = True) -> bytes:
    date = "2026-07-01" if valid else "not-a-date"
    amount = "1.00" if valid else "not-a-number"
    body = "\n".join(f"r{i},{amount},{date}" for i in range(n))
    return f"id,amount,date\n{body}".encode()


def test_an_oversized_upload_is_rejected_before_the_expensive_work(client):
    """The cap has to be cheap, or it is a way to make the server do work for
    free. Rejecting an oversized file should cost about what parsing it costs."""
    blob = csv_rows(api._MAX_ROWS + 5_000)
    t = time.perf_counter()
    r = client.post("/api/reconcile/upload",
                    files={"bank": ("big.csv", io.BytesIO(blob), "text/csv")})
    took = time.perf_counter() - t
    assert r.status_code == 413, f"{r.status_code}: {r.text[:200]}"
    assert took < 10.0, (
        f"rejecting an oversized file took {took:.1f}s — the cap is being applied "
        f"after the mapping work rather than before it"
    )


def test_the_cap_counts_rows_read_not_rows_that_survived(client):
    """A file of mostly-invalid rows used to slip under the cap, because the
    count was taken after validation had thrown most of them away."""
    blob = csv_rows(api._MAX_ROWS + 5_000, valid=False)
    r = client.post("/api/reconcile/upload",
                    files={"bank": ("junk.csv", io.BytesIO(blob), "text/csv")})
    assert r.status_code == 413, (
        f"{api._MAX_ROWS + 5_000} invalid rows returned {r.status_code} — the cap "
        f"counted only the "
        f"survivors"
    )


def test_the_refusal_says_what_to_do_about_it(client):
    r = client.post("/api/reconcile/upload",
                    files={"bank": ("big.csv", io.BytesIO(csv_rows(api._MAX_ROWS + 5_000)),
                                    "text/csv")})
    detail = r.json()["detail"]
    assert "limit" in detail.lower()
    assert "split" in detail.lower(), f"no remedy offered: {detail}"


def test_a_batch_inside_the_cap_still_reconciles(client):
    rows = ("id,amount,date,reference\n"
            + "\n".join(f"r{i},{100 + i}.00,2026-07-01,REF{i:05d}" for i in range(500)))
    r = client.post("/api/reconcile/upload",
                    files={"bank": ("ok.csv", io.BytesIO(rows.encode()), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["metrics"]["total_entries"] == 500


@pytest.mark.parametrize("n", [50, 100, 500, 1_000])
def test_realistic_batch_sizes_are_fast(n):
    """The sizes a merchant actually uploads, end to end through the engine."""
    rows = {
        "payment": [{"id": f"p{i}", "amount": f"{100 + i}.00", "created_at": "2026-07-01",
                     "order_id": f"ORD{i:05d}", "method": "card"} for i in range(n)],
        "ledger": [{"id": f"l{i}", "amount": f"{100 + i}.00", "date": "2026-07-01",
                    "external_ref": f"ORD{i:05d}"} for i in range(n)],
        "settlement": [], "bank": [], "refund": [], "chargeback": [],
    }
    took = elapsed(lambda: run_rows(rows, dataset=f"n{n}", check_replay=False))
    assert took < 5.0, f"{n} rows x 2 sources took {took:.2f}s"
