"""Persistence, the exception state machine, idempotency and carry-forward.

These cover the property that separates a reconciliation *product* from a
reconciliation *script*: work done on an exception survives the next run.
"""
from __future__ import annotations

import pytest

from finance_controller import scenarios
from finance_controller.pipeline import run_rows
from finance_controller.store import Store, WorkflowError, fingerprint


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _run(rows=None, dataset="t"):
    return run_rows(rows or scenarios.combined(), dataset=dataset, check_replay=False)


def _record(store, rows=None, dataset="t", digest="d1"):
    return store.record_run(_run(rows, dataset), digest)


# ------------------------------------------------------------------ idempotency


def test_rerunning_the_same_batch_does_not_duplicate_the_queue(store):
    _record(store)
    first = store.list_exceptions()["total"]
    assert first > 0
    _record(store)
    assert store.list_exceptions()["total"] == first


def test_rerunning_records_a_second_run_because_it_did_happen_twice(store):
    _record(store)
    _record(store)
    assert len(store.list_runs()) == 2


def test_repeated_runs_age_an_exception_rather_than_resetting_it(store):
    _record(store)
    _record(store)
    _record(store)
    seen = {e["times_seen"] for e in store.list_exceptions()["items"]}
    assert seen == {3}


def test_fingerprint_is_stable_and_amount_sensitive():
    a = fingerprint("pay_1", "missing_in_bank", 1000, "INR")
    assert a == fingerprint("pay_1", "missing_in_bank", 1000, "INR")
    # a changed amount is a different piece of work, not a mutation of the old one
    assert a != fingerprint("pay_1", "missing_in_bank", 1001, "INR")
    assert a != fingerprint("pay_1", "missing_in_bank", 1000, "USD")


# ---------------------------------------------------------------- state machine


def test_the_happy_path_through_the_queue(store):
    _record(store)
    eid = store.list_exceptions()["items"][0]["id"]
    store.set_status(eid, "investigating", actor="p")
    store.add_note(eid, "Called the bank.", actor="p")
    store.assign(eid, "praveen", actor="p")
    out = store.set_status(eid, "resolved", actor="p", reason="credit confirmed")
    assert out["status"] == "resolved"
    assert out["assignee"] == "praveen"
    assert out["resolution_reason"] == "credit confirmed"
    assert out["resolved_at"]


def test_illegal_transitions_are_refused(store):
    _record(store)
    eid = store.list_exceptions()["items"][0]["id"]
    store.set_status(eid, "resolved")
    with pytest.raises(WorkflowError):
        store.set_status(eid, "investigating")   # resolved -> investigating


def test_unknown_status_is_refused(store):
    _record(store)
    eid = store.list_exceptions()["items"][0]["id"]
    with pytest.raises(WorkflowError):
        store.set_status(eid, "banana")


def test_every_action_lands_in_history(store):
    _record(store)
    eid = store.list_exceptions()["items"][0]["id"]
    store.set_status(eid, "investigating")
    store.add_note(eid, "note one")
    store.assign(eid, "someone")
    store.set_status(eid, "resolved", reason="done")
    kinds = [h["kind"] for h in store.get_exception(eid)["history"]]
    assert kinds == ["created", "status", "note", "assign", "status"]


def test_an_empty_note_is_refused(store):
    _record(store)
    eid = store.list_exceptions()["items"][0]["id"]
    with pytest.raises(WorkflowError):
        store.add_note(eid, "   ")


def test_actions_on_a_missing_exception_are_refused(store):
    for fn in (
        lambda: store.set_status("nope", "resolved"),
        lambda: store.add_note("nope", "x"),
        lambda: store.assign("nope", "x"),
    ):
        with pytest.raises(WorkflowError):
            fn()


# --------------------------------------------------------------- carry-forward


def test_human_work_survives_the_next_run(store):
    """The property the whole design exists for."""
    _record(store)
    eid = store.list_exceptions()["items"][0]["id"]
    store.set_status(eid, "investigating", actor="p")
    store.add_note(eid, "Bank is tracing the UTR.", actor="p")

    _record(store)          # a whole new reconciliation happens

    after = store.get_exception(eid)
    assert after["status"] == "investigating", "human work was wiped by a re-run"
    assert any(h["kind"] == "note" for h in after["history"])


def test_an_exception_answered_by_later_data_is_auto_resolved(store):
    """The July close flags a sale whose payout never came; the August close,
    which now contains that payout, resolves it -- recording the run that
    explains it rather than letting the item silently vanish.

    The batch is a whole month (not one sale) because "overdue" is relative to
    the end of the period being closed: on 1 July a 1 July sale is simply new.
    """
    august = scenarios.combined()

    july = {k: [dict(r) for r in v] for k, v in august.items()}
    july["settlement"] = [r for r in july["settlement"] if r["id"] != "setl_cf1"]
    july["bank"] = [r for r in july["bank"] if r["id"] != "bank_cf1"]

    store.record_run(_run(july, "july"), "july")
    opened = store.list_exceptions(status="open")["items"]
    target = next(
        (e for e in opened if e["entry_id"] in ("pay_cf1", "ldgr_cf1")), None
    )
    assert target, (
        "a sale booked at the start of July with no payout by month end should "
        f"be on the worklist; queue holds {[e['entry_id'] for e in opened]}"
    )

    # August: the same book, now including the payout that finally arrived
    store.record_run(_run(august, "august"), "august")

    after = store.get_exception(target["id"])
    assert after["status"] == "resolved", (
        "the August payout should have closed the July exception"
    )
    assert "later reconciliation" in (after["resolution_reason"] or "")
    assert any(h["kind"] == "auto_resolved" for h in after["history"])


def test_a_resolved_exception_that_returns_is_reopened(store):
    _record(store)
    eid = store.list_exceptions()["items"][0]["id"]
    store.set_status(eid, "resolved", reason="thought it was fine")
    _record(store)     # same batch: the problem is still there
    after = store.get_exception(eid)
    assert after["status"] == "open"
    assert any(h["kind"] == "reopened" for h in after["history"])


# ------------------------------------------------------------ filter and sort


def test_filtering_and_sorting(store):
    _record(store)
    all_items = store.list_exceptions()["items"]
    cat = all_items[0]["category"]
    assert store.list_exceptions(category=cat)["total"] >= 1
    assert store.list_exceptions(status="open")["total"] >= 1
    assert store.list_exceptions(status="resolved")["total"] == 0

    asc = store.list_exceptions(sort="amount_minor", order="asc")["items"]
    amounts = [e["amount_minor"] for e in asc]
    assert amounts == sorted(amounts)

    biggest = max(e["amount_minor"] for e in all_items)
    assert store.list_exceptions(min_amount=biggest)["total"] >= 1


def test_sort_column_is_whitelisted_against_injection(store):
    _record(store)
    # a hostile sort value must fall back, not reach SQL
    out = store.list_exceptions(sort="amount_minor; DROP TABLE exceptions--")
    assert out["total"] > 0
    assert store.list_exceptions()["total"] > 0     # table still there


def test_search_matches_id_and_reason(store):
    _record(store)
    e = store.list_exceptions()["items"][0]
    assert store.list_exceptions(search=e["entry_id"])["total"] >= 1
    assert store.list_exceptions(search="zzzz-no-such-thing")["total"] == 0


def test_pagination(store):
    _record(store)
    total = store.list_exceptions()["total"]
    page = store.list_exceptions(limit=1, offset=0)
    assert len(page["items"]) == 1
    assert page["total"] == total


def test_priority_reflects_risk(store):
    _record(store)
    items = store.list_exceptions()["items"]
    assert all(e["priority"] in ("low", "medium", "high", "critical") for e in items)


def test_queue_summary_counts(store):
    _record(store)
    s = store.queue_summary()
    assert s["total"] == store.list_exceptions()["total"]
    assert s["open_count"] >= 1
    assert s["open_value_minor"] >= 0
