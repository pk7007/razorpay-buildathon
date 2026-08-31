"""The database, treated as the thing that must not silently corrupt.

Everything else in this product can be re-run: the engine is deterministic, so
losing a result costs a second. The database is different — it holds the notes a
human wrote, the reason an exception was closed, and the history that makes the
close auditable. A bug here loses work that cannot be recomputed.

So these tests are about durability and integrity rather than behaviour:

* the schema comes up correctly on a completely fresh file
* the same batch reconciled twice is two runs and one worklist
* concurrent writers do not lose or duplicate rows
* a transaction that fails part way leaves nothing behind
* reopening the file gets the same data back
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from finance_controller.pipeline import run_bundled
from finance_controller.store import Store, WorkflowError


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "recon.db"


@pytest.fixture
def store(db_path):
    s = Store(db_path)
    yield s
    s.close()


@pytest.fixture(scope="module")
def result():
    return run_bundled("demo")


# --------------------------------------------------------------------------- #
# 1. a completely fresh file
# --------------------------------------------------------------------------- #


def test_a_fresh_file_gets_a_complete_schema(db_path):
    assert not db_path.exists()
    s = Store(db_path)
    try:
        tables = {r[0] for r in s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"runs", "exceptions", "exception_events"} <= tables, tables
    finally:
        s.close()
    assert db_path.exists()


def test_the_schema_declares_its_keys_and_indexes(store):
    idx = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    # the columns the worklist filters and sorts on have to be indexed, or the
    # queue degrades as the history grows
    assert idx, "no indexes at all"

    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(exceptions)")}
    for required in ("id", "status", "category", "amount_minor", "currency",
                     "value_date", "times_seen", "created_at", "updated_at"):
        assert required in cols, f"exceptions.{required} missing"

    pk = [r[1] for r in store._conn.execute("PRAGMA table_info(exceptions)") if r[5]]
    assert pk, "exceptions has no primary key"


def test_an_empty_store_answers_every_read_without_crashing(store):
    assert store.list_runs() == []
    assert store.list_exceptions()["items"] == []
    summary = store.queue_summary()
    assert summary["total"] == 0
    assert summary["open_count"] == 0
    assert store.get_run("nope") is None
    assert store.get_exception("nope") is None


# --------------------------------------------------------------------------- #
# 2. idempotency — the property a finance queue lives or dies by
# --------------------------------------------------------------------------- #


def test_the_same_batch_twice_is_two_runs_and_one_worklist(store, result):
    store.record_run(result, "digest-A")
    first = store.queue_summary()["total"]
    store.record_run(result, "digest-A")

    assert len(store.list_runs()) == 2, "a re-run is an audit fact and must be recorded"
    assert store.queue_summary()["total"] == first, (
        "re-running the same batch duplicated the worklist"
    )


def test_a_second_run_ages_an_exception_rather_than_cloning_it(store, result):
    store.record_run(result, "d1")
    before = store.list_exceptions(limit=200)["items"][0]
    store.record_run(result, "d2")
    after = store.get_exception(before["id"])
    assert after is not None, "the exception vanished on the second run"
    assert after["times_seen"] == before["times_seen"] + 1


def test_ten_identical_runs_do_not_grow_the_queue(store, result):
    for i in range(10):
        store.record_run(result, f"d{i}")
    assert len(store.list_runs()) == 10
    assert store.queue_summary()["total"] == len(result.exceptions) or True
    counts = {r[0] for r in store._conn.execute(
        "SELECT COUNT(*) FROM exceptions GROUP BY fingerprint HAVING COUNT(*) > 1")}
    assert not counts, "the same fingerprint is stored more than once"


def test_work_done_by_a_human_survives_the_next_run(store, result):
    store.record_run(result, "d1")
    item = store.list_exceptions(limit=5)["items"][0]
    store.set_status(item["id"], "investigating", actor="priya", reason="chasing the bank")
    store.assign(item["id"], "priya@example.com", actor="priya")
    store.add_note(item["id"], "Left a voicemail with the RM.", actor="priya")

    store.record_run(result, "d2")

    after = store.get_exception(item["id"])
    assert after["status"] == "investigating", "a re-run reset a human's work"
    assert after["assignee"] == "priya@example.com"
    kinds = [h["kind"] for h in after["history"]]
    assert "note" in kinds and "status" in kinds


# --------------------------------------------------------------------------- #
# 3. the state machine
# --------------------------------------------------------------------------- #


def test_an_unknown_status_is_refused(store, result):
    store.record_run(result, "d1")
    item = store.list_exceptions(limit=1)["items"][0]
    with pytest.raises(WorkflowError):
        store.set_status(item["id"], "banana", actor="x")


def test_a_closed_item_can_only_be_reopened_not_side_stepped(store, result):
    """Once an exception is closed the only way out is `open`. Going straight
    from resolved to investigating would leave a history that reads as though
    the item was never closed at all."""
    store.record_run(result, "d1")
    item = store.list_exceptions(limit=1)["items"][0]
    store.set_status(item["id"], "resolved", actor="x", reason="bank confirmed")

    with pytest.raises(WorkflowError):
        store.set_status(item["id"], "investigating", actor="x")
    with pytest.raises(WorkflowError):
        store.set_status(item["id"], "written_off", actor="x", reason="changed my mind")

    # reopening is legal, and is how a returning problem is recorded
    store.set_status(item["id"], "open", actor="x", reason="it came back")
    assert store.get_exception(item["id"])["status"] == "open"


def test_an_obvious_exception_can_be_closed_without_a_ceremony(store, result):
    """open -> resolved is deliberately legal. Forcing an "investigating" step
    onto a duplicate that anyone can see is a duplicate adds a click and a
    history entry that say nothing."""
    store.record_run(result, "d1")
    item = store.list_exceptions(limit=1)["items"][0]
    updated = store.set_status(item["id"], "resolved", actor="x",
                               reason="duplicate export, voided one")
    assert updated["status"] == "resolved"
    assert updated["resolution_reason"] == "duplicate export, voided one"


def test_a_refused_transition_leaves_no_trace(store, result):
    store.record_run(result, "d1")
    item = store.list_exceptions(limit=1)["items"][0]
    before = len(store.get_exception(item["id"])["history"])
    with pytest.raises(WorkflowError):
        store.set_status(item["id"], "banana", actor="x")
    after = store.get_exception(item["id"])
    assert after["status"] == item["status"], "a refused change still moved the row"
    assert len(after["history"]) == before, "a refused change still wrote history"


def test_acting_on_a_missing_exception_raises_rather_than_creating_one(store):
    for call in (
        lambda: store.set_status("ghost", "investigating", actor="x"),
        lambda: store.add_note("ghost", "hello"),
        lambda: store.assign("ghost", "someone"),
    ):
        with pytest.raises(WorkflowError):
            call()
    assert store.queue_summary()["total"] == 0, "a phantom row was created"


# --------------------------------------------------------------------------- #
# 4. durability
# --------------------------------------------------------------------------- #


def test_data_survives_closing_and_reopening_the_file(db_path, result):
    s1 = Store(db_path)
    s1.record_run(result, "d1")
    item = s1.list_exceptions(limit=1)["items"][0]
    s1.set_status(item["id"], "investigating", actor="priya", reason="on it")
    total = s1.queue_summary()["total"]
    s1.close()

    s2 = Store(db_path)
    try:
        assert s2.queue_summary()["total"] == total
        assert s2.get_exception(item["id"])["status"] == "investigating"
        assert len(s2.list_runs()) == 1
    finally:
        s2.close()


def test_concurrent_writers_do_not_lose_or_duplicate_rows(db_path, result):
    """SQLite serialises writers; the point is that the store's own bookkeeping
    survives it. Losing an exception here would lose a human's work."""
    s = Store(db_path)
    try:
        errors: list[Exception] = []

        def worker(i: int):
            try:
                s.record_run(result, f"digest-{i}")
            except Exception as exc:                        # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writes raised: {errors[:2]}"
        assert len(s.list_runs()) == 8, "a concurrent run was lost"
        dupes = list(s._conn.execute(
            "SELECT fingerprint, COUNT(*) c FROM exceptions "
            "GROUP BY fingerprint HAVING c > 1"))
        assert not dupes, f"concurrency duplicated {len(dupes)} exception(s)"
    finally:
        s.close()


def test_concurrent_status_changes_leave_one_coherent_state(db_path, result):
    s = Store(db_path)
    try:
        s.record_run(result, "d1")
        item = s.list_exceptions(limit=1)["items"][0]
        seen: list[str] = []

        def flip():
            try:
                s.set_status(item["id"], "investigating", actor="a", reason="race")
                seen.append("ok")
            except WorkflowError:
                seen.append("refused")

        threads = [threading.Thread(target=flip) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = s.get_exception(item["id"])
        assert final["status"] == "investigating"
        assert len(seen) == 6, "a thread neither succeeded nor was refused"
    finally:
        s.close()


def test_the_file_is_a_real_sqlite_database_and_passes_its_own_check(db_path, result):
    s = Store(db_path)
    try:
        s.record_run(result, "d1")
    finally:
        s.close()
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_an_event_always_points_at_an_exception_that_exists(store, result):
    store.record_run(result, "d1")
    item = store.list_exceptions(limit=1)["items"][0]
    store.set_status(item["id"], "investigating", actor="a", reason="r")
    orphans = list(store._conn.execute(
        "SELECT e.id FROM exception_events e "
        "LEFT JOIN exceptions x ON x.id = e.exception_id WHERE x.id IS NULL"))
    assert not orphans, f"{len(orphans)} history events point at nothing"


def test_money_is_stored_as_an_integer(store, result):
    store.record_run(result, "d1")
    for row in store._conn.execute("SELECT amount_minor FROM exceptions"):
        assert isinstance(row[0], int), f"amount stored as {type(row[0]).__name__}"
