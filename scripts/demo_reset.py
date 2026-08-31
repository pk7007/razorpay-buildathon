"""Put the app into a known demo state, and prove it is the right one.

    python scripts/demo_reset.py

Run this before recording or presenting. It does four things, in order, and
stops at the first one that does not come out as expected:

    1. moves the existing database aside (never deletes it)
    2. reconciles the demo month into a fresh one
    3. checks every group and every exception against the answer key below
    4. leaves two exceptions worked, so the queue shows a real day's state

The answer key is the point. "It ran without an error" is not a green light
for a demo -- the question is whether it produced the *same* nine groups and
four exceptions it produced last time. Anything that drifts shows up here, in
private, instead of on a shared screen.

    --keep-db     reconcile into the current database instead of a fresh one
    --no-triage   leave every exception open

Nothing here touches Razorpay, the network, or any credential.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")           # type: ignore[union-attr]
else:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from finance_controller.money import fmt  # noqa: E402
from finance_controller.pipeline import run_bundled  # noqa: E402
from finance_controller.store import Store, default_db_path  # noqa: E402

DATASET = "demo"

# ---------------------------------------------------------------- the answer key

# group -> the entries that must be in it, and the status it must carry.
EXPECTED_GROUPS: dict[frozenset[str], str] = {
    frozenset({"pay_norm", "setl_norm", "bank_0", "L-1"}): "complete",
    frozenset({"pay_upi", "setl_upi", "bank_1", "L-2"}): "complete",
    frozenset({"pay_flat", "setl_flat", "bank_2", "L-3"}): "complete",
    frozenset({"pay_big", "setl_big", "bank_3", "L-4"}): "complete",
    frozenset({"pay_refund", "rfnd_part", "setl_refund", "bank_4", "L-5"}): "complete",
    frozenset({"pay_full_refund", "rfnd_full", "L-6"}): "fully_refunded",
    frozenset({"pay_cb", "cbk_1", "setl_cb", "bank_5", "L-7"}): "complete",
    frozenset({"pay_late", "setl_late", "bank_9", "L-8"}): "complete",
    frozenset({"pay_never", "L-9"}): "payout_overdue",
}

EXPECTED_EXCEPTIONS: dict[str, str] = {
    "bank_6": "duplicate",
    "bank_7": "missing_in_ledger",
    "pay_zero": "missing_in_bank",
    "bank_8": "fee_mismatch",
}

# what the queue looks like when the demo starts: one item investigated, one
# resolved, so the worklist shows a day's work in progress rather than a wall
# of untouched rows.
TRIAGE = [
    ("bank_7", "investigating", "Asked the bank for the remitter details on this IMPS credit.",
     "priya@merchant.example"),
    ("bank_6", "resolved", "Confirmed with HDFC: UTR700006 was exported twice. "
     "One credit, booked once.", None),
]

OK, BAD = "PASS", "FAIL"
_failed = False


def say(state: str, title: str, detail: str = "") -> None:
    global _failed
    if state == BAD:
        _failed = True
    print(f"  [{state}] {title}")
    for line in str(detail).splitlines():
        if line:
            print(f"         {line}")


def head(text: str) -> None:
    print(f"\n{text}\n" + "-" * max(len(text), 62))


# ---------------------------------------------------------------- steps


def move_db_aside() -> Path | None:
    """Rename, never delete. A demo reset should not be able to lose data."""
    db = Path(os.getenv("RECON_DB_PATH", "")) if os.getenv("RECON_DB_PATH") else default_db_path()
    if not db.exists():
        say(OK, "No existing database", f"{db} will be created fresh")
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.stem}.{stamp}.bak{db.suffix}")
    shutil.move(str(db), str(backup))
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            side.unlink()
    say(OK, "Existing database moved aside", f"{db.name} -> {backup.name}")
    return backup


def reconcile() -> object:
    result = run_bundled(DATASET)
    m = result.metrics
    say(OK, f"Reconciled the {DATASET} month",
        f"{m.total_entries} entries, {m.groups} groups, {m.exceptions} exceptions, "
        f"auto-match {m.auto_match_rate:.1%}, {m.latency_ms} ms")
    if not m.replay_stable:
        say(BAD, "Replay unstable", "the same input produced a different answer twice")
    return result


def check_groups(result) -> None:
    got = {frozenset(g.entry_ids): g for g in result.groups}
    for members, status in EXPECTED_GROUPS.items():
        g = got.get(members)
        label = " + ".join(sorted(members))
        if g is None:
            near = [set(k) for k in got if set(k) & members]
            say(BAD, f"Group missing: {label}",
                f"closest actual grouping: {near[0] if near else 'none'}")
            continue
        if g.status != status:
            say(BAD, f"Group {g.group_id} has status {g.status!r}, expected {status!r}", label)
            continue
        say(OK, f"{g.group_id}  {status:<15} {label}")

    extra = [g for k, g in got.items() if k not in EXPECTED_GROUPS]
    for g in extra:
        say(BAD, f"Unexpected group {g.group_id}", " + ".join(sorted(g.entry_ids)))


def check_exceptions(result) -> None:
    got = {e.entry_id: e for e in result.exceptions}
    for entry_id, category in EXPECTED_EXCEPTIONS.items():
        e = got.get(entry_id)
        if e is None:
            say(BAD, f"Exception missing for {entry_id}", f"expected {category}")
            continue
        if e.category != category:
            say(BAD, f"{entry_id} is {e.category!r}, expected {category!r}", e.rationale)
            continue
        say(OK, f"{entry_id:<10} {category:<20} {fmt(e.amount_paise)}")
    for entry_id, e in got.items():
        if entry_id not in EXPECTED_EXCEPTIONS:
            say(BAD, f"Unexpected exception {entry_id}", f"{e.category}: {e.rationale}")


def check_conservation(result) -> None:
    grouped = [i for g in result.groups for i in g.entry_ids]
    excepted = [e.entry_id for e in result.exceptions]
    ids = sorted(e.id for e in result.entries)
    if sorted(grouped + excepted) != ids:
        say(BAD, "Conservation broken", "entries were invented or lost")
        return
    if len(grouped) != len(set(grouped)):
        say(BAD, "An entry is in two groups")
        return
    say(OK, "Conservation holds",
        f"all {len(ids)} entries are in exactly one group or one exception")


def record_and_triage(result, triage: bool) -> None:
    store = Store()
    run_id = store.record_run(result, "demo-reset")
    say(OK, "Recorded to the database", f"run {run_id}")

    if not triage:
        return
    queue = {i["entry_id"]: i for i in store.list_exceptions(limit=200)["items"]}
    for entry_id, status, reason, assignee in TRIAGE:
        item = queue.get(entry_id)
        if item is None:
            say(BAD, f"Cannot triage {entry_id}", "it is not in the worklist")
            continue
        # open -> resolved is not a legal single hop; the state machine wants the
        # investigation step in between, which is also how it really happens.
        if status == "resolved" and item["status"] == "open":
            store.set_status(item["id"], "investigating", actor="priya",
                             reason="Picked up during the close.")
        if assignee:
            store.assign(item["id"], assignee, actor="priya")
        store.set_status(item["id"], status, actor="priya", reason=reason)
        say(OK, f"{entry_id} -> {status}", reason)


def show_money(result) -> None:
    mo = result.money
    for label, value in (
        ("gross processed", mo.gross_processed_paise),
        ("reconciled", mo.reconciled_paise),
        ("recoverable", mo.recoverable_paise),
        ("unrecorded", mo.unrecorded_paise),
        ("in exception", mo.in_exception_paise),
    ):
        print(f"    {label:<18} {fmt(value):>18}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset the app to a verified demo state")
    ap.add_argument("--keep-db", action="store_true",
                    help="reconcile into the current database instead of a fresh one")
    ap.add_argument("--no-triage", action="store_true",
                    help="leave every exception open")
    args = ap.parse_args()

    print("=" * 64)
    print("  Demo reset")
    print("=" * 64)

    head("1. Database")
    if args.keep_db:
        say(OK, "Keeping the existing database", "--keep-db was passed")
    else:
        move_db_aside()

    head("2. Reconcile")
    result = reconcile()

    head("3. Groups (expected vs actual)")
    check_groups(result)

    head("4. Exceptions (expected vs actual)")
    check_exceptions(result)

    head("5. Conservation")
    check_conservation(result)

    head("6. Money")
    show_money(result)

    head("7. Persist and triage")
    record_and_triage(result, triage=not args.no_triage)

    head("Verdict")
    if _failed:
        print("  Something above FAILED. Do not demo this state.")
        print("  Fix the difference, or update the answer key in this script if")
        print("  the change was deliberate.\n")
        return 1

    print("  Demo state is verified and ready.\n")
    print("  Start the app:")
    print("    python -m uvicorn finance_controller.api:app --port 8000")
    print("  Then open http://localhost:8000 and go to Overview.\n")
    print("  The story, in order:")
    print("    Overview   -> where the close stands, and what is still open")
    print("    Worklist   -> one item already resolved, one being investigated")
    print("    Reconcile  -> run the demo month live; watch the audit trail")
    print("    Accuracy   -> the held-out numbers, scored on seeds it never saw\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
