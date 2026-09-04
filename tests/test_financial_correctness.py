"""Independent arithmetic audit of the settlement equation.

Every other test in this suite asks "does the engine behave the way the engine
was written to behave?". This file asks a different question: **is the number
right?**

To answer it honestly the expected value has to be computed by something that
does not share code with the thing under test. So `oracle()` below is a
deliberately dumb, hand-written reimplementation of the settlement identity in
plain integers. If `fees.build_breakdown` and `oracle` ever disagree, one of
them is wrong and the test says so — which is the point. A test that computes
the expectation by calling the implementation proves only that the code is
self-consistent.

Two rules hold everywhere here:

* **Integer paise only.** No floats touch a money value, at any point. A test
  that used floats could pass on a machine where the product would be off by a
  paisa, which for a reconciliation tool is a correctness bug, not a rounding
  nicety.
* **Combinations, not features.** Reconciliation breaks where terms interact —
  a refund inside the payout window on a TDS-withheld international card — not
  on any single term. So the cases below are a cross-product, not a checklist.
"""
from __future__ import annotations

import itertools

import pytest

from finance_controller.fees import BPS, FeeRule, FeeSchedule, build_breakdown
from finance_controller.money import fmt


def half_up(numerator: int, denominator: int) -> int:
    """Halves away from zero, in integers — written out again here rather than
    imported, so this file never asserts the implementation against itself."""
    if denominator == 0:
        return 0
    negative = (numerator < 0) != (denominator < 0)
    n, d = abs(numerator), abs(denominator)
    q = (2 * n + d) // (2 * d)
    return -q if negative else q

# --------------------------------------------------------------------------- #
# the oracle: the identity, written out by hand
# --------------------------------------------------------------------------- #


def oracle(
    gross: int,
    *,
    fee_pct_bps: int = 0,
    fee_flat: int = 0,
    gst_bps: int = 1800,
    tds_bps: int = 0,
    refunds: int = 0,
    chargebacks: int = 0,
    adjustments: int = 0,
) -> dict[str, int]:
    """The settlement identity, computed independently of the engine.

        fee  = round(gross x pct) + flat
        gst  = round(fee x gst_rate)
        tds  = round(gross x tds_rate)
        net  = gross - fee - gst - tds - refunds - chargebacks + adjustments

    Halves round away from zero, in integers, because that is the convention
    the engine states and payment processors use. Python's built-in round()
    would round half to even and disagree on exactly the .5 cases.
    """
    fee = half_up(gross * fee_pct_bps, BPS) + fee_flat
    gst = half_up(fee * gst_bps, BPS)
    tds = half_up(gross * tds_bps, BPS)
    net = gross - fee - gst - tds - refunds - chargebacks + adjustments
    return {"fee": fee, "gst": gst, "tds": tds, "net": net}


# --------------------------------------------------------------------------- #
# 1. the case the equation has to get right
# --------------------------------------------------------------------------- #


def test_the_canonical_case():
    """10,000 gross, 200 fee, 36 GST, 100 TDS, 2,000 refund -> 7,664 net.

    Worked by hand:
        10,000.00  gross
          -200.00  fee            2% of 10,000
           -36.00  GST           18% of 200
          -100.00  TDS            1% of 10,000
        -2,000.00  refund
        ---------
         7,664.00  expected settlement
    """
    b = build_breakdown(
        gross_minor=10_000_00,
        method="card",                       # 2% + 18% GST from the rate card
        schedule=FeeSchedule(tds_bps=100),   # 1% TDS under 194-O
        refund_minor=2_000_00,
    )
    assert b.fee_minor == 200_00
    assert b.tax_minor == 36_00
    assert b.tds_minor == 100_00
    assert b.refund_minor == 2_000_00
    assert b.expected_net_minor == 7_664_00, fmt(b.expected_net_minor)

    # and the same number from the independent oracle
    o = oracle(10_000_00, fee_pct_bps=200, tds_bps=100, refunds=2_000_00)
    assert b.expected_net_minor == o["net"]


# --------------------------------------------------------------------------- #
# 2. every combination of every term
# --------------------------------------------------------------------------- #

GROSSES = [1, 99, 100_00, 999_99, 10_000_00, 1_00_00_000, 99_99_99_999]
FEE_RULES = [
    ("upi", 0, 0),            # zero MDR
    ("card", 200, 0),         # 2%
    ("card_intl", 300, 0),    # 3%
    ("netbanking", 180, 0),   # 1.8%
    ("flat", 0, 300),         # flat 3.00
    ("both", 150, 200),       # 1.5% + 2.00
]
TDS_RATES = [0, 100, 50]
DEDUCTIONS = [(0, 0), (1_00, 0), (0, 2_50_00), (5_00_00, 1_00_00)]


@pytest.mark.parametrize("gross", GROSSES)
@pytest.mark.parametrize("method,pct,flat", FEE_RULES)
@pytest.mark.parametrize("tds_bps", TDS_RATES)
@pytest.mark.parametrize("refund,chargeback", DEDUCTIONS)
def test_every_combination_matches_the_oracle(gross, method, pct, flat, tds_bps,
                                              refund, chargeback):
    sched = FeeSchedule(
        rules={method: FeeRule(pct, flat, 1800, method), "default": FeeRule(pct, flat)},
        tds_bps=tds_bps,
    )
    b = build_breakdown(gross_minor=gross, method=method, schedule=sched,
                        refund_minor=refund, chargeback_minor=chargeback)
    o = oracle(gross, fee_pct_bps=pct, fee_flat=flat, tds_bps=tds_bps,
               refunds=refund, chargebacks=chargeback)

    assert b.fee_minor == o["fee"], f"fee: {b.fee_minor} != {o['fee']}"
    assert b.tax_minor == o["gst"], f"gst: {b.tax_minor} != {o['gst']}"
    assert b.tds_minor == o["tds"], f"tds: {b.tds_minor} != {o['tds']}"
    assert b.expected_net_minor == o["net"], b.explain()


# --------------------------------------------------------------------------- #
# 3. properties that must hold for any input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gross", GROSSES)
def test_the_terms_always_sum_back_to_gross(gross):
    """net + every deduction == gross. If this fails, money vanished."""
    b = build_breakdown(gross_minor=gross, method="card",
                        schedule=FeeSchedule(tds_bps=100),
                        refund_minor=min(gross, 1_00), chargeback_minor=0)
    total = (b.expected_net_minor + b.fee_minor + b.tax_minor
             + b.tds_minor + b.refund_minor + b.chargeback_minor
             - b.adjustment_minor)
    assert total == b.gross_minor


def test_no_money_value_is_ever_a_float():
    b = build_breakdown(gross_minor=333_33, method="card", schedule=FeeSchedule(tds_bps=75))
    for field in ("gross_minor", "fee_minor", "tax_minor", "tds_minor",
                  "refund_minor", "chargeback_minor", "adjustment_minor",
                  "expected_net_minor"):
        v = getattr(b, field)
        assert isinstance(v, int), f"{field} is {type(v).__name__}, not int"


def test_zero_gross_produces_zero_everything():
    b = build_breakdown(gross_minor=0, method="card", schedule=FeeSchedule(tds_bps=100))
    assert (b.fee_minor, b.tax_minor, b.tds_minor, b.expected_net_minor) == (0, 0, 0, 0)


def test_a_very_large_amount_stays_exact():
    """Python ints do not overflow, but a float pipeline would lose paise here."""
    gross = 99_99_99_99_999          # just under 1,000 crore in paise
    b = build_breakdown(gross_minor=gross, method="card", schedule=FeeSchedule(tds_bps=100))
    o = oracle(gross, fee_pct_bps=200, tds_bps=100)
    assert b.expected_net_minor == o["net"]
    assert b.expected_net_minor == gross - o["fee"] - o["gst"] - o["tds"]


def test_one_paisa_survives_the_whole_equation():
    b = build_breakdown(gross_minor=1, method="card", schedule=FeeSchedule(tds_bps=100))
    # 2% of 1 paisa rounds to 0; nothing is silently rounded up into existence
    assert b.fee_minor == 0 and b.tax_minor == 0 and b.tds_minor == 0
    assert b.expected_net_minor == 1


def test_a_refund_larger_than_gross_produces_a_negative_expectation():
    """The engine must not clamp. A negative expectation is the signal that the
    source data disagrees with itself, and clamping would hide it."""
    b = build_breakdown(gross_minor=1_000_00, method="upi",
                        schedule=FeeSchedule(), refund_minor=1_500_00)
    assert b.expected_net_minor == -500_00


# --------------------------------------------------------------------------- #
# 4. reported beats estimated, and the taint travels
# --------------------------------------------------------------------------- #


def test_a_reported_fee_wins_and_is_marked_actual():
    b = build_breakdown(gross_minor=10_000_00, method="card",
                        schedule=FeeSchedule(),
                        reported_fee_minor=137_00, reported_tax_minor=24_66)
    assert b.fee_minor == 137_00           # not the rate card's 200_00
    assert b.tax_minor == 24_66
    assert b.fee_provenance == "actual"
    assert not b.is_estimated
    assert b.expected_net_minor == 10_000_00 - 137_00 - 24_66


def test_an_inferred_fee_is_marked_estimated():
    b = build_breakdown(gross_minor=10_000_00, method="card", schedule=FeeSchedule())
    assert b.fee_provenance == "estimated"
    assert b.is_estimated
    assert b.expected().provenance == "estimated"


def test_the_explanation_shows_every_term_that_applies():
    b = build_breakdown(gross_minor=25_000_00, method="card",
                        schedule=FeeSchedule(tds_bps=100), refund_minor=5_000_00,
                        chargeback_minor=1_000_00, adjustment_minor=50_00)
    text = b.explain(actual_net_minor=b.expected_net_minor)
    for term in ("gross", "fee", "tax on fee", "TDS", "refunds",
                 "chargebacks", "adjustments", "expected settlement",
                 "actual settlement", "difference", "MATCHED"):
        assert term in text, f"{term!r} missing from:\n{text}"


def test_a_mismatch_is_labelled_unexplained_not_matched():
    b = build_breakdown(gross_minor=10_000_00, method="card", schedule=FeeSchedule())
    text = b.explain(actual_net_minor=b.expected_net_minor - 1)
    assert "UNEXPLAINED" in text and "MATCHED" not in text


# --------------------------------------------------------------------------- #
# 5. rounding, stated rather than assumed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gross,expected_fee", [
    (100_01, 200),      # 2% of 100.01 = 2.0002 -> 2.00
    (100_25, 201),      # 2% of 100.25 = 2.005  -> 2.01, half away from zero
    (33_33, 67),        # 2% of 33.33  = 0.6666 -> 0.67
    (1_00, 2),          # 2% of 1.00   = 0.02
    (25, 1),            # 2% of 0.25   = 0.005  -> 0.01, not 0.00
    (75, 2),            # 2% of 0.75   = 0.015  -> 0.02, not banker's 0.02/0.01
])
def test_fee_rounding_is_half_away_from_zero(gross, expected_fee):
    rule = FeeRule(200, 0, 1800, "card")
    assert rule.fee_on(gross) == expected_fee


def test_each_row_rounds_independently():
    """Fees are charged per transaction, so they round per transaction. Rounding
    a batch total instead would disagree with the settlement report by a few
    paise on a busy day — and those paise are exactly what this tool exists to
    notice."""
    rows = [25, 75, 1_25, 1_75]          # every one of these is a .5 case at 2%
    rule = FeeRule(200, 0, 1800, "card")
    per_row = sum(rule.fee_on(g) for g in rows)
    assert [rule.fee_on(g) for g in rows] == [1, 2, 3, 4]   # 0.5 -> 1, 1.5 -> 2, ...
    assert per_row == 10
    # rounding the batch total would have produced a different, wrong number
    assert per_row != half_up(sum(rows) * 200, BPS)


# --------------------------------------------------------------------------- #
# 6. the full cross-product, as one property
# --------------------------------------------------------------------------- #


def test_every_combination_agrees_with_the_oracle():
    """A single assertion over the whole space, so a regression anywhere in the
    equation fails one obvious test rather than a scattering of them.

    Named for the space rather than a count: this used to say "two hundred and
    forty" while the product of its own inputs was 504, and a number baked into
    a test name goes stale the moment someone adds a rate to GROSSES.
    """
    disagreements = []
    for gross, (method, pct, flat), tds, (ref, cb) in itertools.product(
        GROSSES, FEE_RULES, TDS_RATES, DEDUCTIONS
    ):
        sched = FeeSchedule(rules={method: FeeRule(pct, flat, 1800, method)}, tds_bps=tds)
        b = build_breakdown(gross_minor=gross, method=method, schedule=sched,
                            refund_minor=ref, chargeback_minor=cb)
        o = oracle(gross, fee_pct_bps=pct, fee_flat=flat, tds_bps=tds,
                   refunds=ref, chargebacks=cb)
        if b.expected_net_minor != o["net"]:
            disagreements.append((gross, method, tds, ref, cb,
                                  b.expected_net_minor, o["net"]))
    assert not disagreements, f"{len(disagreements)} disagreements: {disagreements[:5]}"
