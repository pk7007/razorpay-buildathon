"""Hand-built reconciliation scenarios with exact, hand-checked expected answers.

``synth.py`` generates volume. This module generates *specific financial
situations* -- one refund, one chargeback, one TDS deduction -- where the right
answer is small enough to verify by hand and assert on.

Every scenario is a plain dict of rows in the same shape a real export has, plus
the answer key. They drive ``tests/test_scenarios.py`` and are what the demo
uses to show a single case end to end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

GST_BPS = 1800
START = date(2026, 7, 1)


def _d(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def _r(paise: int) -> str:
    return f"{paise / 100:.2f}"


@dataclass
class Scenario:
    """One reconciliation situation with a verifiable expected outcome."""

    key: str
    title: str
    story: str
    payments: list[dict] = field(default_factory=list)
    settlements: list[dict] = field(default_factory=list)
    bank: list[dict] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    refunds: list[dict] = field(default_factory=list)
    chargebacks: list[dict] = field(default_factory=list)
    # what must be true after reconciliation
    expect_grouped: list[list[str]] = field(default_factory=list)
    expect_exception_categories: dict[str, str] = field(default_factory=dict)
    expect_settled_minor: int | None = None

    def rows(self) -> dict:
        return {
            "payment": self.payments,
            "settlement": self.settlements,
            "bank": self.bank,
            "ledger": self.ledger,
            "refund": self.refunds,
            "chargeback": self.chargebacks,
        }


def _payment(pid, order, gross, day, method="card", currency="INR"):
    return dict(id=pid, amount=_r(gross), created_at=_d(day), order_id=order,
                method=method, status="captured", currency=currency)


def _settlement(sid, utr, net, fee, tax, day, tds=None, currency="INR"):
    row = dict(id=sid, amount=_r(net), fees=_r(fee), tax=_r(tax),
               settled_at=_d(day), utr=utr, currency=currency)
    if tds is not None:
        row["tds"] = _r(tds)
    return row


def _bank(bid, amount, day, utr="", narration="NEFT RAZORPAY", currency="INR"):
    return dict(id=bid, amount=_r(amount), value_date=_d(day), utr=utr,
                narration=narration, type="credit", currency=currency)


def _ledger(lid, amount, day, ref, memo="Sale", currency="INR"):
    return dict(id=lid, amount=_r(amount), date=_d(day), external_ref=ref,
                memo=memo, currency=currency)


def _refund(rid, payment_ref, amount, day, currency="INR"):
    return dict(id=rid, amount=_r(amount), created_at=_d(day),
                payment_id=payment_ref, status="processed", currency=currency)


def _chargeback(cid, payment_ref, amount, day, status="lost", currency="INR"):
    return dict(id=cid, amount=_r(amount), created_at=_d(day),
                payment_id=payment_ref, status=status, currency=currency)


def _fee_pair(gross: int, bps: int) -> tuple[int, int]:
    fee = round(gross * bps / 10_000)
    return fee, round(fee * GST_BPS / 10_000)


# --------------------------------------------------------------------------- scenarios


def _clean_card() -> Scenario:
    gross = 10_000_00
    fee, tax = _fee_pair(gross, 200)
    net = gross - fee - tax
    return Scenario(
        key="clean_card",
        title="Ordinary card sale",
        story="A 10,000 card payment settles T+2 net of a 2% fee and 18% GST.",
        payments=[_payment("pay_c1", "ORD1001", gross, 0, "card")],
        settlements=[_settlement("setl_c1", "UTR1001", net, fee, tax, 2)],
        bank=[_bank("bank_c1", net, 2, "UTR1001")],
        ledger=[_ledger("ldgr_c1", gross, 0, "ORD1001")],
        expect_grouped=[["bank_c1", "ldgr_c1", "pay_c1", "setl_c1"]],
        expect_settled_minor=net,
    )


def _zero_mdr_upi() -> Scenario:
    """UPI carries zero MDR -- proves no global 2% is assumed."""
    gross = 5_000_00
    return Scenario(
        key="zero_mdr_upi",
        title="UPI sale with zero MDR",
        story="UPI has no gateway fee. A flat 2% assumption would break this.",
        payments=[_payment("pay_u1", "ORD2001", gross, 0, "upi")],
        settlements=[_settlement("setl_u1", "UTR2001", gross, 0, 0, 2)],
        bank=[_bank("bank_u1", gross, 2, "UTR2001")],
        ledger=[_ledger("ldgr_u1", gross, 0, "ORD2001")],
        expect_grouped=[["bank_u1", "ldgr_u1", "pay_u1", "setl_u1"]],
        expect_settled_minor=gross,
    )


def _negotiated_rate() -> Scenario:
    """A merchant on 1.4% -- a different rate, same code path."""
    gross = 20_000_00
    fee, tax = _fee_pair(gross, 140)
    net = gross - fee - tax
    return Scenario(
        key="negotiated_rate",
        title="Negotiated 1.4% rate",
        story="Merchant-specific pricing must reconcile without config changes.",
        payments=[_payment("pay_n1", "ORD3001", gross, 0, "card")],
        settlements=[_settlement("setl_n1", "UTR3001", net, fee, tax, 2)],
        bank=[_bank("bank_n1", net, 2, "UTR3001")],
        ledger=[_ledger("ldgr_n1", gross, 0, "ORD3001")],
        expect_grouped=[["bank_n1", "ldgr_n1", "pay_n1", "setl_n1"]],
        expect_settled_minor=net,
    )


def _flat_fee() -> Scenario:
    gross = 1_450_00
    fee = 3_00
    tax = round(fee * GST_BPS / 10_000)
    net = gross - fee - tax
    return Scenario(
        key="flat_fee",
        title="Flat per-transaction fee",
        story="A fixed 3.00 fee rather than a percentage.",
        payments=[_payment("pay_f1", "ORD4001", gross, 0, "netbanking")],
        settlements=[_settlement("setl_f1", "UTR4001", net, fee, tax, 2)],
        bank=[_bank("bank_f1", net, 2, "UTR4001")],
        ledger=[_ledger("ldgr_f1", gross, 0, "ORD4001")],
        expect_grouped=[["bank_f1", "ldgr_f1", "pay_f1", "setl_f1"]],
        expect_settled_minor=net,
    )


def _with_tds() -> Scenario:
    """194-O style withholding on top of fee and GST."""
    gross = 12_500_00
    fee, tax = _fee_pair(gross, 200)
    tds = 100_00
    net = gross - fee - tax - tds
    return Scenario(
        key="with_tds",
        title="TDS withheld at source",
        story="gross - fee - GST - TDS = net. The identity must include TDS.",
        payments=[_payment("pay_t1", "ORD5001", gross, 0, "card")],
        settlements=[_settlement("setl_t1", "UTR5001", net, fee, tax, 2, tds=tds)],
        bank=[_bank("bank_t1", net, 2, "UTR5001")],
        ledger=[_ledger("ldgr_t1", gross, 0, "ORD5001")],
        expect_grouped=[["bank_t1", "ldgr_t1", "pay_t1", "setl_t1"]],
        expect_settled_minor=net,
    )


def _partial_refund() -> Scenario:
    """1000 paid, 300 refunded -> 700 settles. Not an unmatched 1000."""
    gross = 1_000_00
    refund = 300_00
    settled_gross = gross - refund
    fee, tax = _fee_pair(settled_gross, 200)
    net = settled_gross - fee - tax
    return Scenario(
        key="partial_refund",
        title="Partial refund",
        story="1,000 paid, 300 refunded. 700 settles -- the payment is not unmatched.",
        payments=[_payment("pay_r1", "ORD6001", gross, 0, "card")],
        refunds=[_refund("rfnd_r1", "ORD6001", refund, 1)],
        settlements=[_settlement("setl_r1", "UTR6001", net, fee, tax, 3)],
        bank=[_bank("bank_r1", net, 3, "UTR6001")],
        ledger=[_ledger("ldgr_r1", gross, 0, "ORD6001")],
        expect_grouped=[["bank_r1", "ldgr_r1", "pay_r1", "rfnd_r1", "setl_r1"]],
        expect_settled_minor=net,
    )


def _multiple_refunds() -> Scenario:
    """Paid, then refunded twice -> the remainder settles."""
    gross = 1_600_00
    settled_gross = gross - 300_00 - 200_00
    fee, tax = _fee_pair(settled_gross, 200)
    net = settled_gross - fee - tax
    return Scenario(
        key="multiple_refunds",
        title="Two refunds on one payment",
        story="1,600 paid, refunded 300 then 200. 1,100 settles.",
        payments=[_payment("pay_m1", "ORD7001", gross, 0, "card")],
        refunds=[_refund("rfnd_m1", "ORD7001", 300_00, 1),
                 _refund("rfnd_m2", "ORD7001", 200_00, 2)],
        settlements=[_settlement("setl_m1", "UTR7001", net, fee, tax, 4)],
        bank=[_bank("bank_m1", net, 4, "UTR7001")],
        ledger=[_ledger("ldgr_m1", gross, 0, "ORD7001")],
        expect_grouped=[["bank_m1", "ldgr_m1", "pay_m1", "rfnd_m1", "rfnd_m2", "setl_m1"]],
        expect_settled_minor=net,
    )


def _full_refund() -> Scenario:
    """Paid then fully refunded -> nothing settles at all."""
    gross = 1_150_00
    return Scenario(
        key="full_refund",
        title="Full refund",
        story="Fully refunded: 0 settles, and no payout should be expected.",
        payments=[_payment("pay_x1", "ORD8001", gross, 0, "card")],
        refunds=[_refund("rfnd_x1", "ORD8001", gross, 1)],
        ledger=[_ledger("ldgr_x1", gross, 0, "ORD8001")],
        expect_grouped=[["ldgr_x1", "pay_x1", "rfnd_x1"]],
        expect_settled_minor=0,
    )


def _late_refund() -> Scenario:
    """The refund lands in the NEXT period, after the payout already went out."""
    gross = 2_000_00
    fee, tax = _fee_pair(gross, 200)
    net = gross - fee - tax
    return Scenario(
        key="late_refund",
        title="Refund after the payout",
        story="Payout went out on the full amount; the refund arrives 20 days later.",
        payments=[_payment("pay_l1", "ORD9001", gross, 0, "card")],
        settlements=[_settlement("setl_l1", "UTR9001", net, fee, tax, 2)],
        bank=[_bank("bank_l1", net, 2, "UTR9001")],
        ledger=[_ledger("ldgr_l1", gross, 0, "ORD9001")],
        refunds=[_refund("rfnd_l1", "ORD9001", 500_00, 20)],
        expect_grouped=[["bank_l1", "ldgr_l1", "pay_l1", "rfnd_l1", "setl_l1"]],
    )


def _chargeback_lost() -> Scenario:
    gross = 4_000_00
    cb = 4_000_00
    return Scenario(
        key="chargeback_lost",
        title="Chargeback lost",
        story="A disputed card payment is clawed back in full.",
        payments=[_payment("pay_cb1", "ORDA001", gross, 0, "card")],
        chargebacks=[_chargeback("cbk_1", "ORDA001", cb, 10, "lost")],
        ledger=[_ledger("ldgr_cb1", gross, 0, "ORDA001")],
        expect_grouped=[["cbk_1", "ldgr_cb1", "pay_cb1"]],
        expect_settled_minor=0,
    )


def _dispute_open() -> Scenario:
    gross = 3_000_00
    fee, tax = _fee_pair(gross, 200)
    net = gross - fee - tax
    return Scenario(
        key="dispute_open",
        title="Dispute still open",
        story="Money settled, dispute raised but not decided. Must not be lost.",
        payments=[_payment("pay_d1", "ORDB001", gross, 0, "card")],
        settlements=[_settlement("setl_d1", "UTRB001", net, fee, tax, 2)],
        bank=[_bank("bank_d1", net, 2, "UTRB001")],
        ledger=[_ledger("ldgr_d1", gross, 0, "ORDB001")],
        chargebacks=[_chargeback("cbk_d1", "ORDB001", gross, 5, "under_review")],
        expect_grouped=[["bank_d1", "cbk_d1", "ldgr_d1", "pay_d1", "setl_d1"]],
    )


def _orphan_refund() -> Scenario:
    return Scenario(
        key="orphan_refund",
        title="Refund with no payment",
        story="A refund naming a payment that is not in the batch. Must not be guessed.",
        refunds=[_refund("rfnd_o1", "ORD_MISSING", 750_00, 3)],
        expect_exception_categories={"rfnd_o1": "orphan_refund"},
    )


def _over_refund() -> Scenario:
    gross = 1_350_00
    return Scenario(
        key="over_refund",
        title="Refunds exceed the payment",
        story="1,350 paid but 1,500 refunded -- a data error that must be flagged.",
        payments=[_payment("pay_or1", "ORDC001", gross, 0, "card")],
        refunds=[_refund("rfnd_or1", "ORDC001", 900_00, 1),
                 _refund("rfnd_or2", "ORDC001", 600_00, 2)],
        ledger=[_ledger("ldgr_or1", gross, 0, "ORDC001")],
    )


def _multi_currency() -> Scenario:
    """A USD payment and an INR payment of the identical numeric amount."""
    amount = 2_750_00
    fee, tax = _fee_pair(amount, 300)
    net = amount - fee - tax
    return Scenario(
        key="multi_currency",
        title="USD and INR of the same number",
        story="2,750 USD and 2,750 INR must never match each other.",
        payments=[
            _payment("pay_usd", "ORDD001", amount, 0, "card_intl", "USD"),
            _payment("pay_inr", "ORDD002", amount, 0, "card", "INR"),
        ],
        settlements=[_settlement("setl_usd", "UTRD001", net, fee, tax, 2, currency="USD")],
        bank=[_bank("bank_usd", net, 2, "UTRD001", currency="USD")],
        ledger=[_ledger("ldgr_usd", amount, 0, "ORDD001", currency="USD")],
        expect_grouped=[["bank_usd", "ldgr_usd", "pay_usd", "setl_usd"]],
    )


def _carry_forward() -> Scenario:
    """July payment, August payout -- the classic cross-period case."""
    gross = 6_000_00
    fee, tax = _fee_pair(gross, 200)
    net = gross - fee - tax
    return Scenario(
        key="carry_forward",
        title="July payment, August payout",
        story="The payout lands 34 days later, in the next reconciliation period.",
        payments=[_payment("pay_cf1", "ORDE001", gross, 0, "card")],
        ledger=[_ledger("ldgr_cf1", gross, 0, "ORDE001")],
        settlements=[_settlement("setl_cf1", "UTRE001", net, fee, tax, 34)],
        bank=[_bank("bank_cf1", net, 34, "UTRE001")],
        expect_grouped=[["bank_cf1", "ldgr_cf1", "pay_cf1", "setl_cf1"]],
    )


def _assert_distinct_grosses() -> None:
    """Guard: two scenarios sharing a gross on a shared day would collide when
    combined, and the resulting refusal would look like an engine fault rather
    than the fixture coincidence it is."""
    seen: dict[tuple, str] = {}
    for sc in _BUILT:
        for p in sc.payments:
            key = (p["amount"], p["created_at"], p.get("currency", "INR"))
            if key in seen and seen[key] != sc.key:
                raise AssertionError(
                    f"scenarios {seen[key]!r} and {sc.key!r} both have a "
                    f"{p['amount']} payment on {p['created_at']} -- pick another amount"
                )
            seen[key] = sc.key


_BUILT = [
    _clean_card(), _zero_mdr_upi(), _negotiated_rate(), _flat_fee(), _with_tds(),
    _partial_refund(), _multiple_refunds(), _full_refund(), _late_refund(),
    _chargeback_lost(), _dispute_open(), _orphan_refund(), _over_refund(),
    _multi_currency(), _carry_forward(),
]
_assert_distinct_grosses()

ALL: dict[str, Scenario] = {s.key: s for s in _BUILT}


def combined() -> dict:
    """Every scenario merged into one batch -- the demo dataset."""
    out: dict[str, list[dict]] = {
        "payment": [], "settlement": [], "bank": [],
        "ledger": [], "refund": [], "chargeback": [],
    }
    for sc in ALL.values():
        for source, rows in sc.rows().items():
            out[source].extend(rows)
    return out
