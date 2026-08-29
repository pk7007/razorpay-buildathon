"""Held-out generalisation and throughput.

These are the two numbers Track 4 is actually judged on, so they are asserted,
not just printed. The thresholds are deliberately a little below the observed
values — they are regression guards, not the reported result.
"""
from __future__ import annotations

import pytest

from finance_controller.evaluate import (
    DEV_SEED,
    HOLDOUT_SEEDS,
    benchmark,
    evaluate,
    holdout_report,
    score_run,
)


@pytest.fixture(scope="module")
def holdout():
    return evaluate(HOLDOUT_SEEDS)


def test_holdout_seeds_are_not_the_dev_seed():
    """The whole point: nothing here was looked at while writing a rule."""
    assert DEV_SEED not in HOLDOUT_SEEDS


def test_holdout_is_a_meaningful_sample(holdout):
    assert len(holdout.scores) == len(HOLDOUT_SEEDS) * 3
    assert sum(s.entries for s in holdout.scores) > 4_000


def test_precision_holds_on_unseen_data(holdout):
    """A wrong auto-match silently closes the book on real money, so precision
    is the metric that must not slip. Every held-out run, not just the mean."""
    worst = min(s.precision for s in holdout.scores)
    assert worst >= 0.99, f"precision regressed on held-out data: {worst}"


def test_no_money_lands_in_a_wrong_group(holdout):
    total_bad = sum(s.false_match_cost_paise for s in holdout.scores)
    assert total_bad == 0, f"₹{total_bad / 100:,.2f} sitting in incorrect groups"


def test_recall_is_high_on_unseen_data(holdout):
    mean = sum(s.recall for s in holdout.scores) / len(holdout.scores)
    assert mean >= 0.97, f"mean held-out recall {mean}"


def test_generalisation_gap_is_small():
    """Dev-vs-holdout gap is the anti-overfitting check: a big gap would mean the
    rules were fitted to the dev seed rather than to the accounting identities."""
    rep = holdout_report()
    assert rep["generalisation_gap"]["f1"] <= 0.05


def test_every_holdout_run_is_replay_stable(holdout):
    assert all(s.replay_stable for s in holdout.scores)


def test_exception_categories_generalise(holdout):
    scored = [s for s in holdout.scores if s.exception_category_accuracy is not None]
    mean = sum(s.exception_category_accuracy for s in scored) / len(scored)
    assert mean >= 0.90, f"mean held-out exception-category accuracy {mean}"


def test_score_run_is_deterministic():
    a, b = score_run("realistic", 101), score_run("realistic", 101)
    assert (a.precision, a.recall, a.entries) == (b.precision, b.recall, b.entries)


@pytest.mark.slow
def test_throughput_scales():
    """Track 4 names throughput. Assert a floor and that it degrades gracefully."""
    res = benchmark((1_000, 5_000))
    assert res["peak_records_per_sec"] > 3_000
    for run in res["runs"]:
        assert run["records"] >= run["target_records"] * 0.8
        assert run["auto_match_rate"] > 0.9
