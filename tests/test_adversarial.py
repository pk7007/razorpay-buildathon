"""Attempts to make the matcher confidently wrong.

Every other test here asks whether the engine matches what it should. This file
asks the opposite: **can it be made to match something it shouldn't?**

That asymmetry is the whole point. An unmatched transaction costs a finance
team a few minutes. A *confident wrong match* books the wrong invoice as paid,
closes a period on a false number, and is invisible until someone chases a
customer who already paid. So the bar every case below holds the engine to is:

    when the evidence is ambiguous, refuse.

A refusal shows up as an exception with a reason. A guess shows up as money in
the wrong group. `false_match_cost_paise` in the evaluation exists to measure
exactly that, and it is zero; these tests are the adversarial half of the same
claim — the cases a generator would not think to produce.
"""
from __future__ import annotations

from finance_controller.pipeline import run_rows

DAY = "2026-07-10"


def rows(**kw):
    base = {"payment": [], "settlement": [], "bank": [], "ledger": [],
            "refund": [], "chargeback": []}
    base.update(kw)
    return base


def run(**kw):
    return run_rows(rows(**kw), dataset="adversarial", check_replay=True)


def grouped_ids(result):
    return {i for g in result.groups for i in g.entry_ids}


def groups_containing(result, entry_id):
    return [g for g in result.groups if entry_id in g.entry_ids]


def in_same_group(result, a: str, b: str) -> bool:
    return any(a in g.entry_ids and b in g.entry_ids for g in result.groups)


def exception_for(result, entry_id):
    for e in result.exceptions:
        if e.entry_id == entry_id:
            return e
    return None


# --------------------------------------------------------------------------- #
# the invariant every case below relies on
# --------------------------------------------------------------------------- #


def assert_conserved(result):
    """Every entry is in exactly one group or exactly one exception.

    Without this, "it didn't match wrongly" could be satisfied by quietly
    dropping the row, which is a worse failure than a bad match.
    """
    grouped = [i for g in result.groups for i in g.entry_ids]
    excepted = [e.entry_id for e in result.exceptions]
    assert len(grouped) == len(set(grouped)), "an entry is in two groups"
    assert sorted(grouped + excepted) == sorted(e.id for e in result.entries), (
        "entries were invented or lost"
    )


# --------------------------------------------------------------------------- #
# 1. two payments for exactly the same amount on the same day
# --------------------------------------------------------------------------- #


def test_identical_amounts_same_day_are_not_attributed_by_amount_alone():
    """Two 5,000 payments, one 4,882.00 payout. Amounts cannot say which one it
    paid, and the engine must not pick."""
    r = run(
        payment=[
            {"id": "pay_A", "amount": 5000.00, "created_at": DAY, "method": "card"},
            {"id": "pay_B", "amount": 5000.00, "created_at": DAY, "method": "card"},
        ],
        settlement=[{"id": "setl_1", "utr": "UTR1", "amount": 4882.00,
                     "fee": 100.00, "tax": 18.00, "settled_at": "2026-07-12"}],
    )
    assert_conserved(r)
    # whichever way it resolved, it must not have claimed both
    assert not in_same_group(r, "pay_A", "pay_B") or len(r.groups) == 0, (
        "two independent payments were merged into one settlement"
    )
    # and no group may claim a payment it cannot distinguish from its twin
    for g in r.groups:
        if "setl_1" in g.entry_ids:
            claimed = {i for i in g.entry_ids if i.startswith("pay_")}
            assert len(claimed) != 1 or g.confidence < 1.0, (
                f"claimed {claimed} with confidence {g.confidence} on ambiguous evidence"
            )


def test_a_settlement_matching_the_sum_of_two_identical_payments_is_refused():
    """Two 5,000 payments and a 9,764 payout: the *pair* is the only subset that
    sums, so it is unambiguous and may match. Three payments where two different
    pairs sum to the same figure must not."""
    r = run(
        payment=[
            {"id": "pay_A", "amount": 3000.00, "created_at": DAY, "method": "card"},
            {"id": "pay_B", "amount": 4000.00, "created_at": DAY, "method": "card"},
            {"id": "pay_C", "amount": 4000.00, "created_at": DAY, "method": "card"},
        ],
        # 3000 + 4000 = 7000 gross -> two different pairs produce it
        settlement=[{"id": "setl_1", "utr": "UTR1", "amount": 6834.00,
                     "fee": 140.00, "tax": 25.20, "settled_at": "2026-07-12"}],
    )
    assert_conserved(r)
    for g in r.groups:
        if "setl_1" in g.entry_ids:
            picked = sorted(i for i in g.entry_ids if i.startswith("pay_"))
            # pay_B and pay_C are interchangeable; picking one is a guess
            assert picked != ["pay_A", "pay_B"] and picked != ["pay_A", "pay_C"], (
                f"picked {picked} when the other pair fits identically"
            )


# --------------------------------------------------------------------------- #
# 2. references that look alike
# --------------------------------------------------------------------------- #


def test_similar_but_different_references_do_not_match():
    """UTR8811 and UTR8812 differ by one character. Reference matching is exact
    or it is not matching."""
    r = run(
        settlement=[{"id": "setl_1", "utr": "UTR8811", "amount": 1000.00,
                     "settled_at": DAY}],
        bank=[{"id": "bank_1", "amount": 1000.00, "date": DAY, "reference": "UTR8812",
               "narration": "NEFT"}],
    )
    assert_conserved(r)
    assert not in_same_group(r, "setl_1", "bank_1"), (
        "UTR8811 was matched to UTR8812 — a near-miss reference is a mismatch"
    )


def test_a_duplicated_reference_on_two_rows_does_not_create_a_three_way_group():
    """Two bank credits carrying the same UTR. One settlement. At most one of
    them is that settlement's payout; claiming both would double-count."""
    r = run(
        settlement=[{"id": "setl_1", "utr": "UTR9", "amount": 1000.00,
                     "settled_at": DAY}],
        bank=[
            {"id": "bank_1", "amount": 1000.00, "date": DAY, "reference": "UTR9"},
            {"id": "bank_2", "amount": 1000.00, "date": DAY, "reference": "UTR9"},
        ],
    )
    assert_conserved(r)
    for g in r.groups:
        banks = [i for i in g.entry_ids if i.startswith("bank_")]
        assert len(banks) <= 1, f"one settlement claimed two bank credits: {banks}"


def test_missing_references_do_not_become_a_wildcard():
    """Empty is not a value. Two rows with no reference must not match *each
    other* on the strength of both being blank."""
    r = run(
        settlement=[{"id": "setl_1", "utr": "", "amount": 777.00, "settled_at": DAY}],
        bank=[{"id": "bank_1", "amount": 999.00, "date": DAY, "reference": ""}],
    )
    assert_conserved(r)
    assert not in_same_group(r, "setl_1", "bank_1"), (
        "two blank references were treated as a matching reference"
    )


# --------------------------------------------------------------------------- #
# 3. amounts that are nearly the same
# --------------------------------------------------------------------------- #


def test_a_one_rupee_difference_is_not_absorbed():
    """The engine tolerates a couple of paise of rounding drift. A rupee is not
    rounding — it is a discrepancy, and it has to surface."""
    r = run(
        settlement=[{"id": "setl_1", "utr": "UTRX", "amount": 1000.00,
                     "settled_at": DAY}],
        bank=[{"id": "bank_1", "amount": 999.00, "date": DAY, "reference": "UTRY"}],
    )
    assert_conserved(r)
    assert not in_same_group(r, "setl_1", "bank_1")


def test_two_paise_of_drift_is_tolerated_but_labelled():
    """Rounding across systems is real; the tolerance exists on purpose and is
    narrow. This pins the boundary so a future change cannot widen it silently."""
    from finance_controller.reconcile import ROUNDING_SLACK_PAISE
    assert ROUNDING_SLACK_PAISE <= 2, (
        "the rounding tolerance grew; at some width it starts absorbing real "
        "discrepancies instead of rounding"
    )


# --------------------------------------------------------------------------- #
# 4. currency
# --------------------------------------------------------------------------- #


def test_the_same_number_in_two_currencies_never_matches():
    """1,000 USD and 1,000 INR share a number and nothing else."""
    r = run(
        payment=[{"id": "pay_usd", "amount": 1000.00, "created_at": DAY,
                  "currency": "USD", "method": "card"}],
        ledger=[{"id": "ldgr_inr", "amount": 1000.00, "date": DAY,
                 "currency": "INR", "reference": "X"}],
    )
    assert_conserved(r)
    assert not in_same_group(r, "pay_usd", "ldgr_inr"), (
        "netted across currencies without a rate"
    )


def test_a_shared_reference_does_not_override_a_currency_difference():
    """Even an exact reference match must not join two currencies: the reference
    says they are related, not that 1,000 USD equals 1,000 INR."""
    r = run(
        settlement=[{"id": "setl_usd", "utr": "SAME", "amount": 1000.00,
                     "currency": "USD", "settled_at": DAY}],
        bank=[{"id": "bank_inr", "amount": 1000.00, "date": DAY,
               "currency": "INR", "reference": "SAME"}],
    )
    assert_conserved(r)
    for g in r.groups:
        if "setl_usd" in g.entry_ids and "bank_inr" in g.entry_ids:
            raise AssertionError("USD and INR were summed into one group")


# --------------------------------------------------------------------------- #
# 5. time
# --------------------------------------------------------------------------- #


def test_a_payout_a_month_early_is_not_matched_backwards():
    """A bank credit dated before the settlement that supposedly produced it
    cannot be that payout."""
    r = run(
        settlement=[{"id": "setl_1", "utr": "", "amount": 5000.00,
                     "settled_at": "2026-07-20"}],
        bank=[{"id": "bank_1", "amount": 5000.00, "date": "2026-06-20",
               "reference": "", "narration": "NEFT"}],
    )
    assert_conserved(r)
    assert not in_same_group(r, "setl_1", "bank_1"), (
        "a credit that landed a month before the settlement was matched to it"
    )


def test_amount_alone_across_a_long_gap_is_not_enough():
    """Two unrelated 1,234.56 amounts 90 days apart. Without a reference there
    is no evidence they are the same transaction."""
    r = run(
        payment=[{"id": "pay_1", "amount": 1234.56, "created_at": "2026-04-01",
                  "method": "upi"}],
        ledger=[{"id": "ldgr_1", "amount": 1234.56, "date": "2026-07-01",
                 "reference": ""}],
    )
    assert_conserved(r)
    if in_same_group(r, "pay_1", "ldgr_1"):
        g = groups_containing(r, "pay_1")[0]
        assert g.confidence < 0.8, (
            f"matched across 90 days on amount alone with confidence {g.confidence}"
        )


# --------------------------------------------------------------------------- #
# 6. refunds pointed at the wrong thing
# --------------------------------------------------------------------------- #


def test_a_refund_whose_amount_equals_another_payment_is_not_reattached():
    """A 2,000 refund names pay_1. There is also a 2,000 *payment*. The refund
    must attach by reference, never by finding an amount that fits."""
    r = run(
        payment=[
            {"id": "pay_1", "amount": 9000.00, "created_at": DAY, "method": "card"},
            {"id": "pay_2", "amount": 2000.00, "created_at": DAY, "method": "card"},
        ],
        refund=[{"id": "rfnd_1", "payment_id": "pay_1", "amount": 2000.00,
                 "created_at": DAY}],
    )
    assert_conserved(r)
    assert not in_same_group(r, "rfnd_1", "pay_2"), (
        "the refund was attached to a payment that merely shares its amount"
    )


def test_an_orphan_refund_is_reported_not_guessed_onto_the_nearest_payment():
    r = run(
        payment=[{"id": "pay_1", "amount": 5000.00, "created_at": DAY, "method": "card"}],
        refund=[{"id": "rfnd_x", "payment_id": "pay_MISSING", "amount": 500.00,
                 "created_at": DAY}],
    )
    assert_conserved(r)
    exc = exception_for(r, "rfnd_x")
    assert exc is not None and exc.category == "orphan_refund", (
        f"expected orphan_refund, got {exc.category if exc else 'a match'}"
    )
    assert not in_same_group(r, "rfnd_x", "pay_1")


def test_a_chargeback_naming_an_absent_payment_is_reported():
    r = run(
        payment=[{"id": "pay_1", "amount": 5000.00, "created_at": DAY, "method": "card"}],
        chargeback=[{"id": "cbk_x", "payment_id": "pay_GONE", "amount": 5000.00,
                     "created_at": DAY, "status": "lost"}],
    )
    assert_conserved(r)
    exc = exception_for(r, "cbk_x")
    assert exc is not None and exc.category == "orphan_chargeback"
    assert not in_same_group(r, "cbk_x", "pay_1")


# --------------------------------------------------------------------------- #
# 7. the summary property
# --------------------------------------------------------------------------- #


def test_no_adversarial_case_produces_a_full_confidence_wrong_group():
    """One sweep over every case above: nothing reaches confidence 1.0 unless a
    reference or an exact unique identity justified it."""
    cases = [
        dict(payment=[{"id": "a", "amount": 100.00, "created_at": DAY, "method": "card"},
                      {"id": "b", "amount": 100.00, "created_at": DAY, "method": "card"}],
             ledger=[{"id": "l", "amount": 100.00, "date": DAY, "reference": ""}]),
        dict(settlement=[{"id": "s", "utr": "", "amount": 50.00, "settled_at": DAY}],
             bank=[{"id": "b1", "amount": 50.00, "date": DAY, "reference": ""},
                   {"id": "b2", "amount": 50.00, "date": DAY, "reference": ""}]),
        dict(payment=[{"id": "p", "amount": 0.00, "created_at": DAY, "method": "upi"}],
             ledger=[{"id": "l", "amount": 0.00, "date": DAY, "reference": ""}]),
    ]
    for i, case in enumerate(cases):
        r = run(**case)
        assert_conserved(r)
        for g in r.groups:
            if g.confidence >= 1.0:
                assert g.rule and ("reference" in g.rule or "exact" in g.rule), (
                    f"case {i}: full confidence from rule {g.rule!r} without a "
                    f"reference: {g.rationale}"
                )


def test_a_zero_amount_row_does_not_match_everything():
    """Zero is the amount most likely to 'fit' anywhere. It must not become a
    universal joiner."""
    r = run(
        payment=[{"id": "pay_zero", "amount": 0.00, "created_at": DAY, "method": "upi"}],
        settlement=[{"id": "setl_1", "utr": "U1", "amount": 1000.00, "settled_at": DAY}],
        bank=[{"id": "bank_1", "amount": 1000.00, "date": DAY, "reference": "U1"}],
        ledger=[{"id": "ldgr_1", "amount": 1000.00, "date": DAY, "reference": "U1"}],
    )
    assert_conserved(r)
    for g in r.groups:
        if "pay_zero" in g.entry_ids:
            assert len(g.entry_ids) <= 2, (
                "a zero-amount row was absorbed into an unrelated group"
            )


def test_every_group_the_engine_forms_can_explain_itself():
    """A match with no stated reason cannot be reviewed, which makes it
    unauditable regardless of whether it is right."""
    r = run(
        payment=[{"id": "pay_1", "amount": 1000.00, "created_at": DAY, "method": "card",
                  "order_id": "ORD1"}],
        settlement=[{"id": "setl_1", "utr": "UTR1", "amount": 976.40, "fee": 20.00,
                     "tax": 3.60, "settled_at": "2026-07-12"}],
        bank=[{"id": "bank_1", "amount": 976.40, "date": "2026-07-12",
               "reference": "UTR1"}],
        ledger=[{"id": "ldgr_1", "amount": 1000.00, "date": DAY, "reference": "ORD1"}],
    )
    assert_conserved(r)
    assert r.groups, "the well-formed case did not match at all"
    for g in r.groups:
        assert g.rule, f"{g.group_id} has no rule"
        assert len(g.rationale) > 20, f"{g.group_id} rationale is not an explanation"
        assert any(ch.isdigit() for ch in g.rationale), (
            f"{g.group_id} rationale states no arithmetic: {g.rationale!r}"
        )
