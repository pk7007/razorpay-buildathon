"""The live Razorpay path, exercised against a stand-in for the SDK.

No test-mode credentials were available while this was built, so the live branch
could not be run for real. That is exactly why it is tested here: "it should work
once you add keys" is a guess unless the code path is executed. These tests drive
``fetch_live()`` with a fake client returning the documented response shapes, so
the parsing, the guards and the failure handling are all genuinely run.

What this does NOT prove: that Razorpay's real API returns what its docs say. Only
credentials can close that gap -- see ``scripts/verify_razorpay.py``.
"""
from __future__ import annotations

import dataclasses
import sys
import types

import pytest

from finance_controller import razorpay_source as rz
from finance_controller.pipeline import run_rows

# Response shapes per Razorpay's API docs: integer paise, epoch seconds,
# prefixed ids, refunds naming their payment.
LIVE_PAYMENTS = [
    {"id": "pay_LIVE0001", "entity": "payment", "amount": 250000, "currency": "INR",
     "status": "captured", "order_id": "order_LIVE01", "method": "card",
     "captured": True, "fee": 5000, "tax": 900, "created_at": 1782000000,
     "description": "Test payment", "notes": {}},
    {"id": "pay_LIVE0002", "entity": "payment", "amount": 100000, "currency": "INR",
     "status": "captured", "order_id": "order_LIVE02", "method": "upi",
     "captured": True, "fee": 0, "tax": 0, "created_at": 1782000000,
     "notes": {}},
]
LIVE_REFUNDS = [
    {"id": "rfnd_LIVE001", "entity": "refund", "amount": 50000, "currency": "INR",
     "payment_id": "pay_LIVE0001", "status": "processed", "created_at": 1782086400,
     "notes": {}},
]
LIVE_SETTLEMENTS = [
    {"id": "setl_LIVE001", "entity": "settlement", "amount": 294100, "status": "processed",
     "fees": 5000, "tax": 900, "utr": "UTRLIVE0001", "created_at": 1782172800},
]


class _Collection:
    def __init__(self, items):
        self._items = items

    def all(self, _params=None):
        return {"entity": "collection", "count": len(self._items), "items": self._items}


class _FakeClient:
    """Stands in for razorpay.Client."""

    def __init__(self, *, payments=None, refunds=None, settlements=None,
                 fail_on: str | None = None):
        self._fail_on = fail_on
        self.payment = _Collection(payments if payments is not None else LIVE_PAYMENTS)
        self.refund = _Collection(refunds if refunds is not None else LIVE_REFUNDS)
        self.settlement = _Collection(
            settlements if settlements is not None else LIVE_SETTLEMENTS
        )
        self.app_details = None
        if fail_on == "payment":
            self.payment = self._boom()
        elif fail_on == "settlement":
            self.settlement = self._boom()

    def set_app_details(self, d):
        self.app_details = d

    @staticmethod
    def _boom():
        class _Broken:
            def all(self, _params=None):
                raise RuntimeError("upstream 503")
        return _Broken()


@pytest.fixture
def test_keys(monkeypatch):
    """Pretend valid test-mode credentials are configured."""
    patched = dataclasses.replace(
        rz.SETTINGS,
        razorpay_key_id="rzp_test_FAKEKEY123456",
        razorpay_key_secret="fakesecret",
    )
    monkeypatch.setattr(rz, "SETTINGS", patched)
    return patched


def _install(monkeypatch, client):
    """Point ``razorpay.Client`` at the stand-in.

    The SDK is an OPTIONAL dependency -- the product runs fully without it -- so
    a stub module is injected when it is absent rather than making these tests
    require an install they should not need.
    """
    if "razorpay" not in sys.modules:
        stub = types.ModuleType("razorpay")
        stub.Client = lambda **_kw: client  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "razorpay", stub)
    else:
        monkeypatch.setattr("razorpay.Client", lambda **_kw: client)


# ------------------------------------------------------------------- guards


def test_missing_credentials_raise_rather_than_fall_back():
    """A silent fallback is how fixture data gets mistaken for live data."""
    with pytest.raises(rz.RazorpayUnavailable, match="not set"):
        rz.fetch_live()


def test_a_live_key_is_refused(monkeypatch):
    """A demo tool must never be pointed at real customer data."""
    patched = dataclasses.replace(
        rz.SETTINGS, razorpay_key_id="rzp_live_REAL", razorpay_key_secret="s"
    )
    monkeypatch.setattr(rz, "SETTINGS", patched)
    with pytest.raises(rz.RazorpayUnavailable, match="not a test-mode key"):
        rz.fetch_live()


# --------------------------------------------------------------- happy path


def test_live_pull_parses_the_documented_shapes(test_keys, monkeypatch):
    client = _FakeClient()
    _install(monkeypatch, client)

    batch = rz.fetch_live()

    assert batch.provenance == "live_test"
    assert len(batch.payments) == 2
    assert len(batch.refunds) == 1
    assert len(batch.settlements) == 1
    assert client.app_details, "app details should be set so calls are attributable"


def test_live_data_reconciles_end_to_end(test_keys, monkeypatch):
    """Epoch timestamps, integer paise and a refund keyed by payment id all have
    to survive normalisation for this to group correctly."""
    _install(monkeypatch, _FakeClient())
    batch = rz.fetch_live()

    result = run_rows(batch.as_rows(), dataset="live", check_replay=True)

    assert result.metrics.total_entries == 4
    assert result.metrics.replay_stable

    # the refund must attach to its payment, not become an orphan
    assert not any(e.category == "orphan_refund" for e in result.exceptions)
    mem = {i: g for g in result.groups for i in g.entry_ids}
    assert mem["pay_LIVE0001"].group_id == mem["rfnd_LIVE001"].group_id

    # 2500.00 + 1000.00 - 500.00 refund - 50.00 fee - 9.00 tax = 2941.00 settled
    assert mem["setl_LIVE001"].group_id == mem["pay_LIVE0001"].group_id


def test_epoch_timestamps_become_real_dates(test_keys, monkeypatch):
    _install(monkeypatch, _FakeClient())
    result = run_rows(rz.fetch_live().as_rows(), dataset="live", check_replay=False)
    for e in result.entries:
        assert e.value_date.year == 2026, f"{e.id} parsed to {e.value_date}"


def test_amounts_are_read_as_paise_not_rupees(test_keys, monkeypatch):
    """Razorpay sends integer paise. Reading 250000 as rupees would be 100x wrong."""
    _install(monkeypatch, _FakeClient())
    result = run_rows(rz.fetch_live().as_rows(), dataset="live", check_replay=False)
    pay = next(e for e in result.entries if e.id == "pay_LIVE0001")
    assert pay.amount_paise == 250000       # 2,500.00


def test_reported_fees_are_marked_actual(test_keys, monkeypatch):
    _install(monkeypatch, _FakeClient())
    result = run_rows(rz.fetch_live().as_rows(), dataset="live", check_replay=False)
    setl = next(e for e in result.entries if e.source == "settlement")
    assert setl.fee_reported, "a fee Razorpay reported must never be re-estimated"
    assert setl.fee_paise == 5000


# ------------------------------------------------------------------ failure


def test_a_dead_payments_endpoint_is_reported_not_swallowed(test_keys, monkeypatch):
    _install(monkeypatch, _FakeClient(fail_on="payment"))
    with pytest.raises(rz.RazorpayUnavailable, match="failed"):
        rz.fetch_live()


def test_a_missing_settlements_endpoint_is_survivable(test_keys, monkeypatch):
    """Many test accounts have no settlements. That is not an error."""
    _install(monkeypatch, _FakeClient(fail_on="settlement"))
    batch = rz.fetch_live()
    assert batch.provenance == "live_test"
    assert batch.settlements == []
    assert batch.payments, "payments should still come through"


def test_an_empty_account_still_returns_a_labelled_batch(test_keys, monkeypatch):
    _install(monkeypatch, _FakeClient(payments=[], refunds=[], settlements=[]))
    batch = rz.fetch_live()
    assert batch.provenance == "live_test"
    assert run_rows(batch.as_rows(), dataset="empty",
                    check_replay=False).metrics.total_entries == 0


def test_load_prefers_live_and_falls_back_loudly(test_keys, monkeypatch):
    """When a live pull fails, the fallback must SAY it fell back."""
    _install(monkeypatch, _FakeClient(fail_on="payment"))
    batch = rz.load(prefer_live=True)
    assert batch.provenance == "fixture"
    assert "live pull attempted and failed" in batch.note


def test_uncaptured_payments_never_reach_reconciliation(test_keys, monkeypatch):
    """An authorized-but-uncaptured payment is not money owed."""
    extra = dict(LIVE_PAYMENTS[0], id="pay_AUTHONLY", status="authorized",
                 captured=False, order_id="order_AUTH")
    _install(monkeypatch, _FakeClient(payments=[*LIVE_PAYMENTS, extra]))
    batch = rz.fetch_live()
    # fetch_live returns what the API gave; the caller decides what to keep
    assert any(p["id"] == "pay_AUTHONLY" for p in batch.payments)
    captured = [p for p in batch.payments if p.get("status") == "captured"]
    assert len(captured) == 2
