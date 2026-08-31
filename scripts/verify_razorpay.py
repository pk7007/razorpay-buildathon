"""Prove the Razorpay test-mode path works, end to end, in one command.

    python scripts/verify_razorpay.py

Every step reports PASS / FAIL / SKIP with the exact next action, so a failure
tells you what to do rather than what broke. Nothing here fabricates data: if
the credentials are missing or the API is unreachable, it says so and stops
instead of quietly falling back to fixtures.

It walks the whole chain, not just the API call:

    Razorpay test mode -> ingestion -> column mapping -> validation
                       -> reconciliation -> SQLite -> the endpoints the UI reads

and writes the evidence to ``out/razorpay-verification.json`` so the claim can
be checked later without re-running anything.

Add --fixtures to run the identical pipeline against local fixtures, which is
useful for checking the ingestion path before any keys exist. The verdict then
says so in as many words: fixtures are never reported as live data.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
else:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from finance_controller.config import SETTINGS  # noqa: E402
from finance_controller.money import fmt  # noqa: E402
from finance_controller.pipeline import run_rows  # noqa: E402
from finance_controller.quality import combined_summary, validate  # noqa: E402
from finance_controller.razorpay_source import (  # noqa: E402
    RazorpayUnavailable,
    fetch_live,
    fixture_batch,
)
from finance_controller.store import Store  # noqa: E402

OK, BAD, SKIP = "PASS", "FAIL", "SKIP"
_failed = False


def say(state: str, title: str, detail: str = "", fix: str = "") -> None:
    global _failed
    if state == BAD:
        _failed = True
    print(f"  [{state}] {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}")
    if fix:
        print(f"         -> {fix}")


def head(text: str) -> None:
    print(f"\n{text}\n" + "-" * max(len(text), 60))


def check_credentials() -> bool:
    head("1. Credentials")

    if not SETTINGS.razorpay_key_id or not SETTINGS.razorpay_key_secret:
        say(SKIP, "No Razorpay credentials configured",
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set.",
            "Create .env from .env.example and add your TEST-MODE keys from "
            "dashboard.razorpay.com -> Settings -> API Keys (Test Mode).")
        return False

    key = SETTINGS.razorpay_key_id
    say(OK, "Key present", f"{key[:12]}... (secret is set, never printed)")

    if not key.startswith("rzp_test_"):
        say(BAD, "This is not a test-mode key",
            f"Key starts with {key[:8]!r}, expected 'rzp_test_'.",
            "Use test-mode keys only. This tool refuses live keys on purpose: "
            "a demo must never touch real customer data.")
        return False

    say(OK, "Key is test mode", "rzp_test_ prefix confirmed")
    return True


def check_sdk() -> bool:
    head("2. SDK")
    try:
        import razorpay
    except ImportError:
        say(BAD, "razorpay package not installed", fix="pip install razorpay")
        return False
    say(OK, "razorpay installed", getattr(razorpay, "__version__", "version unknown"))
    return True


def pull_live():
    head("3. Test-mode API pull")
    try:
        batch = fetch_live()
    except RazorpayUnavailable as exc:
        say(BAD, "Could not reach Razorpay test mode", str(exc),
            "Check the key/secret pair, and that this machine has outbound "
            "HTTPS to api.razorpay.com.")
        return None

    say(OK, "Connected", f"provenance = {batch.provenance}")
    counts = f"{len(batch.payments)} payments, {len(batch.refunds)} refunds, " \
             f"{len(batch.settlements)} settlements"
    if not any((batch.payments, batch.refunds, batch.settlements)):
        say(BAD, "The account returned no data", counts,
            "A brand-new test account is empty. Create a few test payments "
            "first: razorpay.com/docs/payments/payments/test-card-details")
        return None
    say(OK, "Data returned", counts)

    if not batch.settlements:
        say(SKIP, "No settlements in this account",
            "Payments will reconcile against each other, but the payout leg "
            "cannot be checked.",
            "Test-mode settlements often need to be triggered from the "
            "dashboard. Not a failure -- just a smaller demo.")
    return batch


def check_quality(rows: dict) -> dict:
    head("4. Ingestion quality")
    reports, clean = {}, {}
    for source, raw in rows.items():
        accepted, rep = validate(source, raw)
        clean[source] = accepted
        if raw:
            reports[source] = rep
    summary = combined_summary(reports)
    say(OK if summary["invalid_rows"] == 0 else BAD,
        f"{summary['valid_rows']}/{summary['total_rows']} rows usable",
        f"invalid={summary['invalid_rows']} duplicates={summary['duplicate_rows']} "
        f"empty={summary['empty_rows']} currencies={summary['currencies']}",
        "" if summary["invalid_rows"] == 0
        else "Inspect the per-source report; bad rows are quarantined, not dropped.")
    for src, rep in reports.items():
        if rep.issues:
            worst = rep.issues[0]
            say(SKIP, f"{src}: {len(rep.issues)} issue(s)",
                f"first: row {worst.row} {worst.field} -- {worst.problem}")
    return clean


def reconcile(rows: dict, label: str):
    head("5. Reconciliation")
    result = run_rows(rows, dataset=label, check_replay=True)
    m = result.metrics
    say(OK, f"{m.total_entries} entries reconciled",
        f"{m.groups} groups, {m.exceptions} exceptions, "
        f"auto-match {m.auto_match_rate:.1%}, {m.latency_ms} ms")
    say(OK if m.replay_stable else BAD, "Replay stable",
        "re-running produced an identical result" if m.replay_stable
        else "the same input produced a different result -- this is a bug")

    matched = [i for g in result.groups for i in g.entry_ids]
    exc = [e.entry_id for e in result.exceptions]
    conserved = sorted(matched + exc) == sorted(e.id for e in result.entries)
    say(OK if conserved else BAD, "Conservation",
        "every entry is in exactly one group or one exception" if conserved
        else "entries were invented or lost -- this is a bug")

    head("6. What it found")
    mo = result.money
    for name, value in (
        ("gross processed", mo.gross_processed_paise),
        ("reconciled", mo.reconciled_paise),
        ("in transit", mo.in_transit_paise),
        ("recoverable", mo.recoverable_paise),
        ("unrecorded", mo.unrecorded_paise),
    ):
        print(f"    {name:<18} {fmt(value):>16}")

    if result.groups:
        print("\n    groups:")
        for g in result.groups[:6]:
            print(f"      {g.group_id}  {g.status:<20} {fmt(g.amount_paise):>13}  "
                  f"{'+'.join(g.sources)}")
    if result.exceptions:
        print("\n    exceptions (each with a reason and an action):")
        for e in result.exceptions[:6]:
            print(f"      {e.entry_id:<24} {e.category:<18} {fmt(e.amount_paise):>13}")
            print(f"        {e.rationale[:88]}")
    return result


def persist(result, provenance: str):
    """Leg 5: the run has to survive the process, or the workflow is a fiction."""
    head("7. Database")
    try:
        store = Store()
        run_id = store.record_run(result, f"razorpay-{provenance}")
    except Exception as exc:  # noqa: BLE001
        say(BAD, "Could not persist the run", f"{type(exc).__name__}: {exc}",
            "The reconciliation worked; the workflow half did not. Check "
            "RECON_DB_PATH and that the directory is writable.")
        return None

    stored = store.get_run(run_id)
    if stored is None:
        say(BAD, "The run was written but cannot be read back", f"run {run_id}")
        return None
    if stored["entries"] != result.metrics.total_entries:
        say(BAD, "The stored run disagrees with the result",
            f"{stored['entries']} entries stored, {result.metrics.total_entries} reconciled")
        return None
    say(OK, "Run recorded and read back", f"run {run_id}, {stored['entries']} entries")

    queue = store.list_exceptions(limit=200)
    summary = store.queue_summary()
    say(OK, f"{summary['total']} item(s) in the worklist",
        f"open={summary['by_status'].get('open', 0)} "
        f"carried_forward={summary['carried_forward']} "
        f"value_at_stake={fmt(summary['open_value_minor'])}")
    for item in queue["items"][:3]:
        print(f"      {item['entry_id']:<24} {item['category']:<18} "
              f"{fmt(item['amount_minor']):>14}")
        print(f"        {item['rationale'][:88]}")
    return {"run_id": run_id, "queue_total": summary["total"], "store": store}


def check_ui_surface(run_id: str):
    """Leg 6: the endpoints the console actually calls, hit in-process."""
    head("8. The surface the UI reads")
    try:
        from fastapi.testclient import TestClient

        from finance_controller.api import app
    except Exception as exc:  # noqa: BLE001
        say(SKIP, "Cannot exercise the API in-process", f"{type(exc).__name__}: {exc}")
        return {}

    seen = {}
    with TestClient(app) as c:
        for path in ("/api/health", "/api/razorpay/status", "/api/runs?limit=5",
                     "/api/exceptions/summary", "/api/exceptions?limit=5"):
            r = c.get(path)
            if r.status_code != 200:
                say(BAD, f"{path} -> {r.status_code}", r.text[:200])
                continue
            seen[path] = r.json()
        say(OK, "Every endpoint the console calls responded",
            ", ".join(sorted(seen)))

        runs = seen.get("/api/runs?limit=5") or []
        if not any(r["id"] == run_id for r in runs):
            say(BAD, "The new run is not in /api/runs",
                "the UI would not show it")
        else:
            say(OK, "The run is visible to the UI", f"run {run_id}")
    return seen


def write_evidence(batch, result, stored, ui) -> Path:
    head("9. Evidence")
    out = Path(__file__).resolve().parents[1] / "out"
    out.mkdir(exist_ok=True)
    path = out / "razorpay-verification.json"
    m, mo = result.metrics, result.money
    payload = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": batch.provenance,
        "is_live_razorpay_test_mode": batch.provenance == "live_test",
        "source_counts": {
            "payments": len(batch.payments),
            "refunds": len(batch.refunds),
            "settlements": len(batch.settlements),
        },
        "reconciliation": {
            "entries": m.total_entries,
            "groups": m.groups,
            "exceptions": m.exceptions,
            "auto_match_rate": m.auto_match_rate,
            "replay_stable": m.replay_stable,
            "latency_ms": m.latency_ms,
        },
        "money_paise": {
            "gross_processed": mo.gross_processed_paise,
            "reconciled": mo.reconciled_paise,
            "recoverable": mo.recoverable_paise,
            "in_exception": mo.in_exception_paise,
        },
        "audit_records": len(result.audit),
        "persisted": bool(stored),
        "run_id": (stored or {}).get("run_id"),
        "worklist_items": (stored or {}).get("queue_total"),
        "ui_endpoints_ok": sorted(ui or {}),
        "note": (
            "Data pulled from a Razorpay TEST-MODE account."
            if batch.provenance == "live_test"
            else "LOCAL FIXTURES in Razorpay's documented response shape. "
                 "This is NOT Razorpay data."
        ),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    say(OK, "Written", str(path.relative_to(Path.cwd())) if path.is_relative_to(Path.cwd())
        else str(path))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the Razorpay ingestion path")
    ap.add_argument("--fixtures", action="store_true",
                    help="use local fixtures instead of the live API "
                         "(checks the pipeline without credentials)")
    args = ap.parse_args()

    print("=" * 62)
    print("  Razorpay ingestion check")
    print("=" * 62)

    if args.fixtures:
        head("1-3. Source")
        batch = fixture_batch()
        say(SKIP, "Using LOCAL FIXTURES, not Razorpay", batch.note)
    else:
        if not check_credentials():
            print("\nRun with --fixtures to exercise the same pipeline without keys.\n")
            return 1
        if not check_sdk():
            return 1
        batch = pull_live()
        if batch is None:
            return 1

    clean = check_quality(batch.as_rows())
    result = reconcile(clean, f"razorpay-{batch.provenance}")
    stored = persist(result, batch.provenance)
    ui = check_ui_surface(stored["run_id"]) if stored else {}
    write_evidence(batch, result, stored, ui)

    head("Verdict")
    if _failed:
        print("  Something above FAILED. Fix it before quoting these numbers.\n")
        return 1

    if batch.provenance == "live_test":
        print("  VERIFIED against Razorpay test-mode data.")
        print("  This is data Razorpay generated, not data this repo generated.\n")
        print("  Next:")
        print("    1. Say exactly this in the video and the panel.")
        print("    2. README -> 'Known limitations': remove the line saying the")
        print("       LLM/Razorpay path is unverified, and state what you ran.")
        print("    3. Keep the keys out of git. .env is already gitignored.\n")
    else:
        print("  Pipeline works on Razorpay-SHAPED data (local fixtures).")
        print("  This is NOT evidence the live integration works.\n")
        print("  To close that gap:")
        print("    1. dashboard.razorpay.com -> Settings -> API Keys -> Test Mode")
        print("    2. cp .env.example .env  and paste the rzp_test_ key + secret")
        print("    3. Create a couple of test payments so the account is not empty")
        print("    4. python scripts/verify_razorpay.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
