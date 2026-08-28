"""Engine correctness on the bundled benchmark datasets + conservation invariants."""
from __future__ import annotations

import pytest

from finance_controller.ingest import available_datasets
from finance_controller.pipeline import run_bundled, run_rows

DATASETS = ["clean", "realistic", "messy"]


@pytest.fixture(scope="module", params=DATASETS)
def result(request):
    return run_bundled(request.param), request.param


def test_datasets_present():
    assert set(DATASETS) <= set(available_datasets())


def test_perfect_precision_and_recall(result):
    r, name = result
    m = r.metrics
    assert m.precision == 1.0, f"{name}: precision {m.precision}"
    assert m.recall == 1.0, f"{name}: recall {m.recall}"
    assert m.f1 == 1.0


def test_exception_categories_match_truth(result):
    r, name = result
    if name == "clean":
        pytest.skip("clean has no injected exceptions")
    assert r.metrics.exception_category_accuracy == 1.0


def test_conservation(result):
    """Every entry is matched exactly once OR is an exception exactly once."""
    r, _ = result
    matched = [i for g in r.groups for i in g.entry_ids]
    exc = [e.entry_id for e in r.exceptions]
    assert sorted(matched + exc) == sorted(e.id for e in r.entries)
    assert len(matched) == len(set(matched)), "an entry appears in two groups"


def test_replay_is_stable(result):
    r, _ = result
    assert r.metrics.replay_stable is True


def test_every_group_has_two_plus_entries(result):
    r, _ = result
    assert all(len(g.entry_ids) >= 2 for g in r.groups)


def test_every_exception_is_explained(result):
    r, _ = result
    for e in r.exceptions:
        assert e.rationale and e.suggested_action
        assert 0.0 <= e.confidence <= 1.0


def test_offline_resolver_is_used_without_a_key(result):
    r, _ = result
    assert r.resolver_mode == "heuristic"
    assert r.metrics.llm_calls == 0


def test_resolver_actually_contributes_matches(result):
    """The anomaly datasets include off-gateway transfers that only the residual
    resolver can pair — deterministic + structural rules leave them as singletons."""
    r, name = result
    if name == "clean":
        pytest.skip("clean has no resolver-only scenarios")
    assert r.metrics.resolver_share > 0
    assert any(g.stage in ("heuristic", "agent") for g in r.groups)


def test_money_summary_is_coherent(result):
    r, _ = result
    mo = r.money
    assert mo.gross_processed_paise > 0
    assert mo.reconciled_paise <= mo.gross_processed_paise
    for field in ("reconciled_paise", "recoverable_paise", "unrecorded_paise", "in_transit_paise"):
        assert getattr(mo, field) >= 0


def test_empty_input_does_not_crash():
    r = run_rows({"payment": [], "settlement": [], "bank": [], "ledger": []}, dataset="empty")
    assert r.groups == []
    assert r.exceptions == []
    assert r.metrics.total_entries == 0


def test_garbage_rows_are_tolerated():
    rows = {
        "payment": [{"id": "p1", "amount": "not-a-number", "created_at": "nonsense"}],
        "bank": [{"id": "b1", "amount": "", "value_date": ""}],
        "settlement": [],
        "ledger": [],
    }
    r = run_rows(rows, dataset="garbage")
    assert r.metrics.total_entries == 2  # parsed, not crashed
