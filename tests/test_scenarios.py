"""The financial scenarios, asserted.

Each test corresponds to a situation a real merchant month contains, with a
hand-checked expected answer in ``scenarios.py``. These are the tests that would
catch a change that quietly breaks refunds or TDS while the aggregate accuracy
numbers still look fine.
"""
from __future__ import annotations

import pytest

from finance_controller import scenarios
from finance_controller.fees import FeeRule, FeeSchedule, build_breakdown
from finance_controller.money import Amount, CurrencyMismatch, fmt
from finance_controller.pipeline import run_rows


def _run(key: str):
    sc = scenarios.ALL[key]
    return sc, run_rows(sc.rows(), dataset=key, check_replay=False)


def _grouped_together(result, ids: list[str]) -> bool:
    mem = {e: g.group_id for g in result.groups for e in g.entry_ids}
    got = {mem.get(i) for i in ids}
    return len(got) == 1 and None not in got


@pytest.mark.parametrize("key", list(scenarios.ALL))
def test_every_scenario_meets_its_expectation(key):
    sc, r = _run(key)
    for want in sc.expect_grouped:
        placed = [
            (i, next((g.group_id for g in r.groups if i in g.entry_ids), "EXCEPTION"))
            for i in want
        ]
        assert _grouped_together(r, want), (
            f"{key}: {want} should reconcile together, got {placed}"
        )
    got = {e.entry_id: e.category for e in r.exceptions}
    for eid, cat in sc.expect_exception_categories.items():
        assert got.get(eid) == cat, f"{key}: {eid} expected {cat}, got {got.get(eid)}"


@pytest.mark.parametrize("key", list(scenarios.ALL))
def test_conservation_holds_for_every_scenario(key):
    """No entry is invented, duplicated or lost -- in any financial situation."""
    _, r = _run(key)
    matched = [i for g in r.groups for i in g.entry_ids]
    exc = [e.entry_id for e in r.exceptions]
    assert sorted(matched + exc) == sorted(e.id for e in r.entries)
    assert len(matched) == len(set(matched))


# --------------------------------------------------------------------- refunds


def test_partial_refund_reduces_the_settled_amount():
    """1,000 paid, 300 refunded -> 700 settles, and nothing is an exception."""
    sc, r = _run("partial_refund")
    assert r.exceptions == []
    ent = {e.id: e for e in r.entries}
    assert ent["rfnd_r1"].amount_paise == 300_00
    settled = ent["setl_r1"]
    gross_after_refund = 1_000_00 - 300_00
    assert settled.amount_paise + settled.fee_paise + settled.tax_paise == gross_after_refund


def test_two_refunds_on_one_payment_both_deduct():
    sc, r = _run("multiple_refunds")
    assert r.exceptions == []
    ent = {e.id: e for e in r.entries}
    s = ent["setl_m1"]
    assert s.amount_paise + s.fee_paise + s.tax_paise == 1_000_00 - 300_00 - 200_00


def test_full_refund_means_nothing_settles():
    sc, r = _run("full_refund")
    g = r.groups[0]
    assert {"payment", "refund", "ledger"} <= {
        e.source for e in r.entries if e.id in g.entry_ids
    }
    # a fully refunded sale must not be reported as an unpaid payout
    assert all(e.category != "missing_in_bank" for e in r.exceptions)


def test_a_refund_after_the_payout_does_not_shrink_it():
    """The money was already out the door; a later refund cannot retroactively
    reduce that payout, or every late refund breaks its own settlement."""
    sc, r = _run("late_refund")
    assert r.exceptions == []
    ent = {e.id: e for e in r.entries}
    s = ent["setl_l1"]
    # settled on the FULL 2,000, not 1,500
    assert s.amount_paise + s.fee_paise + s.tax_paise == 2_000_00


def test_orphan_refund_is_not_guessed_onto_a_payment():
    sc, r = _run("orphan_refund")
    assert r.groups == []
    assert [e.category for e in r.exceptions] == ["orphan_refund"]


def test_over_refund_is_flagged():
    """1,000 paid but 1,200 refunded is a data error, and must be visible."""
    _, r = _run("over_refund")
    assert any("exceed" in a.rationale for a in r.audit), (
        "over-refunding was not recorded anywhere"
    )


# ---------------------------------------------------------------- chargebacks


def test_lost_chargeback_deducts():
    _, r = _run("chargeback_lost")
    ent = {e.id: e for e in r.entries}
    assert ent["cbk_1"].dispute_status == "lost"
    assert _grouped_together(r, ["pay_cb1", "cbk_1"])


def test_open_dispute_does_not_deduct():
    """Money is still with the merchant while a dispute is under review; treating
    it as clawed back would report every live dispute as a shortfall."""
    _, r = _run("dispute_open")
    ent = {e.id: e for e in r.entries}
    assert ent["cbk_d1"].dispute_status == "under_review"
    s = ent["setl_d1"]
    assert s.amount_paise + s.fee_paise + s.tax_paise == 3_000_00
    assert r.exceptions == []


# ----------------------------------------------------------- fees, TDS, rates


def test_no_global_fee_rate_is_assumed():
    """UPI at zero MDR, a negotiated 1.4%, and a flat fee all reconcile."""
    for key in ("zero_mdr_upi", "negotiated_rate", "flat_fee"):
        _, r = _run(key)
        assert r.exceptions == [], f"{key} produced exceptions"


def test_tds_is_part_of_the_identity():
    _, r = _run("with_tds")
    assert r.exceptions == []
    ent = {e.id: e for e in r.entries}
    s = ent["setl_t1"]
    assert s.tds_paise == 100_00
    assert s.amount_paise + s.fee_paise + s.tax_paise + s.tds_paise == 10_000_00


def test_reported_fees_beat_the_rate_card():
    b = build_breakdown(gross_minor=1_000_00, method="card", reported_fee_minor=1_23,
                        reported_tax_minor=22)
    assert b.fee_minor == 1_23
    assert b.fee_provenance == "actual"
    assert not b.is_estimated


def test_inferred_fees_are_marked_estimated_and_stay_estimated():
    b = build_breakdown(gross_minor=1_000_00, method="card")
    assert b.is_estimated
    assert "estimated" in b.explain()


def test_fee_rules_support_percent_flat_and_both():
    assert FeeRule(200, 0).fee_on(1_000_00) == 20_00
    assert FeeRule(0, 300).fee_on(1_000_00) == 3_00
    assert FeeRule(150, 200).fee_on(1_000_00) == 15_00 + 2_00


def test_settlement_equation_explains_itself():
    b = build_breakdown(
        gross_minor=10_000_00, method="card", reported_fee_minor=200_00,
        reported_tax_minor=36_00, reported_tds_minor=100_00,
    )
    assert b.expected_net_minor == 9_664_00
    text = b.explain(9_664_00)
    for token in ("gross", "fee", "TDS", "expected settlement", "difference", "MATCHED"):
        assert token in text


def test_tds_rate_is_configuration_not_code():
    s = FeeSchedule(tds_bps=250)
    assert s.tds_on(10_000_00) == 250_00
    assert FeeSchedule().tds_on(10_000_00) == 0


# ------------------------------------------------------------- multi-currency


def test_same_number_in_two_currencies_never_matches():
    _, r = _run("multi_currency")
    assert not _grouped_together(r, ["pay_usd", "pay_inr"])
    ent = {e.id: e for e in r.entries}
    assert ent["pay_usd"].currency == "USD"
    assert ent["pay_inr"].currency == "INR"


def test_amounts_in_different_currencies_cannot_be_added():
    with pytest.raises(CurrencyMismatch):
        Amount(1000, "INR") + Amount(1000, "USD")


def test_currency_formatting_respects_minor_units():
    assert fmt(123456, "INR") == "₹1,234.56"
    assert fmt(5000, "JPY") == "¥5,000"          # no minor unit
    assert fmt(1234567, "KWD").endswith("1,234.57")   # three


# ----------------------------------------------------------- carry-forward


def test_july_payment_august_payout_still_matches():
    """The single most common cross-period case: a payout well beyond T+2."""
    _, r = _run("carry_forward")
    assert _grouped_together(r, ["pay_cf1", "setl_cf1", "bank_cf1", "ldgr_cf1"])
    assert any("late-payout" in g.rule for g in r.groups)


def test_late_payout_still_requires_an_exact_unique_amount():
    """The widened window must not become a licence to guess."""
    sc = scenarios.ALL["carry_forward"]
    rows = {k: [dict(r) for r in v] for k, v in sc.rows().items()}
    rows["settlement"][0]["amount"] = "1.00"     # no longer reconciles
    r = run_rows(rows, dataset="broken", check_replay=False)
    assert not _grouped_together(r, ["pay_cf1", "setl_cf1"])


# ------------------------------------------------------------------- combined


def test_the_whole_demo_batch_reconciles_cleanly():
    r = run_rows(scenarios.combined(), dataset="demo", check_replay=False)
    cats = {e.category for e in r.exceptions}
    # only the deliberately-unmatchable ones survive
    assert cats <= {"orphan_refund", "missing_in_bank"}, cats
    assert r.metrics.auto_match_rate > 0.85
