"""The AI is bounded, and this is where that claim is enforced.

The architectural claim the project makes is narrow and testable:

    deterministic and structural rules decide first, and what they decide is
    final; a model only ever sees what is left over, and can only ever add
    groups from that leftover.

That is worth very little as a sentence in a README. These tests make it a
property of the code:

* the model is handed *only* the residual — never an entry a rule already placed
* switching the model on cannot change, reorder or remove a deterministic result
* a model that hallucinates an id, claims an entry twice, or returns rubbish
  loses the entire proposal rather than corrupting the run
* whether a model ran at all is recorded per run, not inferred

A model that reruns the whole batch and "improves" the matching would be a
different product with a different risk profile. The point of the boundary is
that a wrong answer from the model can cost recall, and can never cost
precision.
"""
from __future__ import annotations

import dataclasses

import pytest

from finance_controller import resolver as resolver_mod
from finance_controller.config import SETTINGS
from finance_controller.pipeline import run_bundled, run_rows

DATASETS = ["demo", "clean", "realistic", "messy"]


# --------------------------------------------------------------------------- #
# a stand-in model, so the boundary is testable without a key or a network call
# --------------------------------------------------------------------------- #


def enable_llm(monkeypatch, llm):
    """Point the resolver at a stand-in model.

    ``SETTINGS`` is a frozen dataclass and ``has_llm`` is derived from whether a
    key is present, so the switch is flipped by swapping in a copy that has one
    — never by writing a real key anywhere.
    """
    fake_settings = dataclasses.replace(SETTINGS, anthropic_api_key="test-key-not-real")
    monkeypatch.setattr(resolver_mod, "SETTINGS", fake_settings)
    monkeypatch.setattr(resolver_mod, "_llm_resolve", llm)
    return fake_settings


@pytest.fixture
def spy(monkeypatch):
    """Turn the LLM path on and record exactly what the model was shown."""
    calls: dict = {"residual": None}

    def recorder(residual, audit):
        calls["residual"] = [e.id for e in residual]
        return [], residual, resolver_mod._empty_usage()

    enable_llm(monkeypatch, recorder)
    return calls


# --------------------------------------------------------------------------- #
# 1. the model only ever sees the residual
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dataset", DATASETS)
def test_the_model_is_never_shown_an_entry_a_rule_already_placed(dataset, spy):
    """Whatever the deterministic and structural stages matched is out of scope
    for the model — it is not asked to review, confirm or revisit it."""
    result = run_bundled(dataset)
    shown = set(spy["residual"] or [])
    if not shown:
        pytest.skip(f"{dataset} left nothing residual")

    rule_matched = {
        i for g in result.groups
        for i in g.entry_ids
        if g.stage in ("deterministic", "structural")
    }
    assert not (shown & rule_matched), (
        f"the model was shown {sorted(shown & rule_matched)}, which rules had "
        f"already decided"
    )


@pytest.mark.parametrize("dataset", DATASETS)
def test_the_residual_is_a_small_minority_of_the_batch(dataset, spy):
    """The claim is 'AI on the uncertain tail', so the tail has to be a tail.
    If a model were seeing most of the batch, the architecture diagram would be
    decoration."""
    result = run_bundled(dataset)
    shown = len(spy["residual"] or [])
    total = result.metrics.total_entries
    assert shown <= total * 0.15, (
        f"{shown} of {total} entries ({shown / total:.0%}) reached the model — "
        f"that is not a residual"
    )


# --------------------------------------------------------------------------- #
# 2. turning the model on cannot change a deterministic result
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dataset", DATASETS)
def test_deterministic_groups_are_identical_with_the_model_on_and_off(dataset, monkeypatch):
    without = run_bundled(dataset)

    def silent_llm(residual, audit):
        return [], residual, resolver_mod._empty_usage()

    enable_llm(monkeypatch, silent_llm)
    with_model = run_bundled(dataset)

    def locked(result):
        return sorted(
            (tuple(sorted(g.entry_ids)), g.stage, g.rule, g.status)
            for g in result.groups
            if g.stage in ("deterministic", "structural")
        )

    assert locked(without) == locked(with_model), (
        "enabling the model changed a group that rules had already decided"
    )


@pytest.mark.parametrize("dataset", DATASETS)
def test_a_model_that_proposes_nothing_costs_nothing_but_recall(dataset, monkeypatch):
    """The worst honest outcome of a silent model is more exceptions, never a
    wrong match and never a lost entry."""
    def silent_llm(residual, audit):
        return [], residual, resolver_mod._empty_usage()

    enable_llm(monkeypatch, silent_llm)
    r = run_bundled(dataset)

    grouped = [i for g in r.groups for i in g.entry_ids]
    excepted = [e.entry_id for e in r.exceptions]
    assert sorted(grouped + excepted) == sorted(e.id for e in r.entries)
    if r.metrics.precision is not None:
        assert r.metrics.precision == 1.0


# --------------------------------------------------------------------------- #
# 3. a misbehaving model loses its proposal, not the run
# --------------------------------------------------------------------------- #


def _tiny_batch():
    return {
        "payment": [
            {"id": "p1", "amount": 1000.00, "created_at": "2026-07-01", "method": "card"},
            {"id": "p2", "amount": 2000.00, "created_at": "2026-07-01", "method": "card"},
        ],
        "settlement": [], "bank": [], "ledger": [], "refund": [], "chargeback": [],
    }


def _run_with_llm(monkeypatch, llm):
    enable_llm(monkeypatch, llm)
    return run_rows(_tiny_batch(), dataset="ai-boundary", check_replay=False)


def test_an_invented_entry_id_is_refused(monkeypatch):
    """The model returns an id that was never in the batch."""
    def hallucinating(residual, audit):
        from finance_controller.models import MatchGroup
        return ([MatchGroup(group_id="X1", entry_ids=["p1", "ghost_999"],
                            stage="agent", rule="llm@test", confidence=0.9,
                            rationale="invented")],
                [], resolver_mod._empty_usage())

    r = _run_with_llm(monkeypatch, hallucinating)
    ids = {e.id for e in r.entries}
    for g in r.groups:
        assert set(g.entry_ids) <= ids, f"{g.entry_ids} contains an id that does not exist"
    grouped = [i for g in r.groups for i in g.entry_ids]
    assert sorted(grouped + [e.entry_id for e in r.exceptions]) == sorted(ids), (
        "conservation broke when the model hallucinated"
    )


def test_the_same_entry_claimed_twice_is_refused(monkeypatch):
    def greedy(residual, audit):
        from finance_controller.models import MatchGroup
        return ([MatchGroup(group_id="X1", entry_ids=["p1", "p2"], stage="agent",
                            rule="llm@test", confidence=0.9, rationale="a"),
                 MatchGroup(group_id="X2", entry_ids=["p1"], stage="agent",
                            rule="llm@test", confidence=0.9, rationale="b")],
                [], resolver_mod._empty_usage())

    r = _run_with_llm(monkeypatch, greedy)
    grouped = [i for g in r.groups for i in g.entry_ids]
    assert len(grouped) == len(set(grouped)), "an entry ended up in two groups"


def test_a_model_that_raises_falls_back_and_says_so(monkeypatch):
    def broken(residual, audit):
        raise RuntimeError("model unavailable")

    r = _run_with_llm(monkeypatch, broken)
    grouped = [i for g in r.groups for i in g.entry_ids]
    assert sorted(grouped + [e.entry_id for e in r.exceptions]) == sorted(
        e.id for e in r.entries
    ), "a model failure lost entries"
    fallbacks = [a for a in r.audit if "fallback" in a.rule]
    assert fallbacks, "the fallback happened silently — the audit trail must record it"
    assert "model unavailable" in fallbacks[0].rationale


def test_an_oversized_residual_never_reaches_the_model(monkeypatch):
    """A batch far larger than one merchant-month is not sent: it would cost
    real money for a worse answer than the deterministic scorer."""
    seen = {"called": False}

    def should_not_run(residual, audit):
        seen["called"] = True
        return [], residual, resolver_mod._empty_usage()

    enable_llm(monkeypatch, should_not_run)
    monkeypatch.setattr(resolver_mod, "_MAX_RESIDUAL_FOR_LLM", 3)

    rows = {
        "payment": [
            {"id": f"p{i}", "amount": 100.00 + i, "created_at": "2026-07-01",
             "method": "card"}
            for i in range(8)
        ],
        "settlement": [], "bank": [], "ledger": [], "refund": [], "chargeback": [],
    }
    r = run_rows(rows, dataset="oversized", check_replay=False)
    assert not seen["called"], "an oversized residual was sent to the model anyway"
    skipped = [a for a in r.audit if "oversized" in a.rule]
    assert skipped, "the skip was not recorded"


# --------------------------------------------------------------------------- #
# 4. whether a model ran is recorded, not inferred
# --------------------------------------------------------------------------- #


def test_a_run_states_which_resolver_produced_it():
    r = run_bundled("demo")
    assert r.resolver_mode in ("heuristic", "llm")
    assert r.metrics.llm_calls == 0 or r.resolver_mode == "llm"


def test_with_no_key_configured_nothing_is_attributed_to_a_model(monkeypatch):
    monkeypatch.setattr(
        resolver_mod, "SETTINGS",
        dataclasses.replace(SETTINGS, anthropic_api_key=""),
    )
    r = run_bundled("demo")
    assert r.resolver_mode == "heuristic"
    assert r.metrics.llm_calls == 0
    assert r.metrics.llm_cost_usd == 0.0
    assert not [a for a in r.audit if a.stage == "agent"], (
        "an 'agent' decision was recorded with no model configured"
    )


@pytest.mark.parametrize("dataset", DATASETS)
def test_the_share_each_stage_decided_adds_up(dataset):
    """The dashboard shows structural / deterministic / resolver shares. They
    are the basis of the architecture claim, so they have to be real."""
    m = run_bundled(dataset).metrics
    total = m.deterministic_share + m.structural_share + m.resolver_share
    assert abs(total - 1.0) < 0.01, (
        f"stage shares sum to {total}, so the split shown to a judge is not real"
    )
