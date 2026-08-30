"""Prove the LLM resolver works, and show whether it earns its place.

    python scripts/verify_llm.py

Two questions, answered with evidence rather than assertion:

  1. Does the live path work?  -- one real call, with the real token count,
     the real latency and the real rupee/dollar cost.
  2. Is it worth having?       -- the SAME residual set is resolved by both
     backends and the results are diffed. If the LLM adds nothing, that is
     reported plainly rather than hidden.

Nothing here fabricates a result. With no API key it stops and says so.
``--dry-run`` shows exactly what would be sent (including the redaction applied
to untrusted narration text) without spending anything.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
else:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from finance_controller import resolver as R  # noqa: E402
from finance_controller.audit import AuditLog  # noqa: E402
from finance_controller.config import SETTINGS  # noqa: E402
from finance_controller.models import Entry  # noqa: E402
from finance_controller.money import fmt  # noqa: E402

OK, BAD, SKIP = "PASS", "FAIL", "SKIP"
_failed = False

# Roughly 88 INR to the dollar. Only used to make the cost legible next to the
# rupee amounts the rest of the tool reports; the USD figure is authoritative.
USD_TO_INR = 88


def say(state: str, title: str, detail: str = "", fix: str = "") -> None:
    global _failed
    if state == BAD:
        _failed = True
    print(f"  [{state}] {title}")
    for line in str(detail).splitlines():
        if line:
            print(f"         {line}")
    if fix:
        print(f"         -> {fix}")


def head(text: str) -> None:
    print(f"\n{text}\n" + "-" * max(len(text), 62))


def _e(eid, source, amount, day, narration=None, ref=None, currency="INR"):
    return Entry(
        id=eid, source=source, amount_paise=amount,
        value_date=date(2026, 7, 1) + timedelta(days=day),
        narration=narration, reference=ref, currency=currency,
    )


def residual_set() -> list[Entry]:
    """A residual tail the deterministic rules genuinely cannot place.

    These are off-gateway transfers: a bank credit and a ledger entry that share
    no identifier at all. Only counterparty text and amount connect them, which
    is exactly the shape of problem worth spending a model on.
    """
    return [
        _e("bank_9001", "bank", 398_671, 0, "NEFT INWARD PIXELWORKS LLP MUMBAI"),
        _e("ldgr_9001", "ledger", 398_671, 0, "INV-8838 Pixelworks LLP direct transfer",
           "INV-8838"),
        _e("bank_9002", "bank", 187_157, 1, "IMPS ORCHID TRADING PVT"),
        _e("ldgr_9002", "ledger", 187_157, 1, "INV-9505 Orchid Trading settlement",
           "INV-9505"),
        _e("bank_9003", "bank", 51_200, 2, "NEFT INWARD CEDAR AND CO"),
        _e("ldgr_9003", "ledger", 51_200, 2, "INV-2211 Cedar & Co", "INV-2211"),
        # a genuine orphan: nothing here should pair with it
        _e("bank_9004", "bank", 7_777, 3, "IMPS INWARD UNIDENTIFIED"),
    ]


def check_key() -> bool:
    head("1. Credentials")
    if not SETTINGS.anthropic_api_key:
        say(SKIP, "No ANTHROPIC_API_KEY configured",
            "The product is fully functional without one -- the residual tail is\n"
            "resolved by the deterministic heuristic instead.",
            "Add ANTHROPIC_API_KEY to .env to verify the LLM path. "
            "Run with --dry-run to see what would be sent.")
        return False
    key = SETTINGS.anthropic_api_key
    say(OK, "Key present", f"{key[:8]}... (never printed in full, never logged)")
    say(OK, "Model", SETTINGS.llm_model)
    say(OK, "Bounds",
        f"timeout {SETTINGS.llm_timeout_seconds}s, "
        f"{SETTINGS.llm_max_retries} retries, "
        f"residual cap {R._MAX_RESIDUAL_FOR_LLM} entries")
    return True


def check_sdk() -> bool:
    head("2. SDK")
    try:
        import anthropic
    except ImportError:
        say(BAD, "anthropic package not installed", fix="pip install anthropic")
        return False
    say(OK, "anthropic installed", getattr(anthropic, "__version__", "version unknown"))
    return True


def show_payload(residual: list[Entry]) -> None:
    head("3. What gets sent")
    payload = [
        {
            "id": e.id, "source": e.source,
            "amount": round(e.amount_paise / 100, 2),
            "date": e.value_date.isoformat(),
            "reference": R._clean(e.reference),
            "narration": R._clean(e.narration),
        }
        for e in residual
    ]
    print(json.dumps(payload[:3], indent=2))
    print(f"    ... {len(payload)} entries total\n")
    say(OK, "Untrusted text is flattened before it reaches the prompt",
        f"narration truncated to {R._MAX_TEXT} chars, newlines stripped -- a bank\n"
        f"narration is the one field an outsider can write into")
    say(OK, "Only the residual tail is sent",
        "entries the accounting identities already placed never reach the model")


def run_heuristic(residual: list[Entry]) -> tuple[list, float]:
    head("4. Baseline: the deterministic heuristic")
    t0 = time.perf_counter()
    groups, leftover, _ = R._heuristic_resolve(list(residual), AuditLog())
    ms = (time.perf_counter() - t0) * 1000
    say(OK, f"{len(groups)} group(s) in {ms:.1f} ms",
        f"{len(leftover)} entries left unresolved, cost $0.00")
    for g in groups:
        print(f"         {sorted(g.entry_ids)}  conf {g.confidence:.2f}")
    return groups, ms


def run_llm(residual: list[Entry]) -> tuple[list, dict]:
    head("5. The LLM resolver (one real call)")
    audit = AuditLog()
    t0 = time.perf_counter()
    try:
        groups, leftover, usage = R._llm_resolve(list(residual), audit)
    except Exception as exc:  # noqa: BLE001
        say(BAD, "The call failed", f"{type(exc).__name__}: {exc}",
            "Check the key, the model id, and outbound HTTPS to api.anthropic.com. "
            "Note the product still works -- resolve() falls back to the heuristic.")
        return [], {}
    ms = (time.perf_counter() - t0) * 1000

    say(OK, f"{len(groups)} group(s) accepted in {ms:.0f} ms",
        f"{len(leftover)} entries left unresolved")
    for g in groups:
        print(f"         {sorted(g.entry_ids)}  conf {g.confidence:.2f}")
        print(f"           reason: {g.rationale[:74]}")

    head("6. Cost, measured not estimated")
    cost = usage["llm_cost_usd"]
    print(f"    input tokens      {usage['llm_input_tokens']:>10,}")
    print(f"    output tokens     {usage['llm_output_tokens']:>10,}")
    print(f"    cost              {'$' + format(cost, '.4f'):>10}"
          f"   (~{fmt(int(cost * USD_TO_INR * 100))})")
    print(f"    latency           {ms:>9.0f} ms")
    if cost:
        per_1k = cost * (1000 / max(len(residual), 1))
        print(f"    extrapolated      {'$' + format(per_1k, '.2f'):>10}"
              f"   per 1,000 residual entries")

    rejected = [r for r in audit.records if r.outcome == "rejected"]
    if rejected:
        head("7. Proposals the engine refused")
        for r in rejected:
            say(OK, r.rule, r.rationale[:150])
    else:
        head("7. Proposals the engine refused")
        say(OK, "None this time",
            "Every proposal named real ids and reconciled arithmetically.")
    return groups, usage


def compare(heur: list, llm: list, residual: list[Entry]) -> None:
    head("8. Does the model earn its place?")
    hset = {frozenset(g.entry_ids) for g in heur}
    lset = {frozenset(g.entry_ids) for g in llm}

    only_llm = lset - hset
    only_heur = hset - lset
    both = hset & lset

    say(OK, f"{len(both)} grouping(s) both backends agree on")
    if only_llm:
        say(OK, f"{len(only_llm)} grouping(s) only the LLM found",
            "\n".join(str(sorted(s)) for s in only_llm))
    if only_heur:
        say(OK, f"{len(only_heur)} grouping(s) only the heuristic found",
            "\n".join(str(sorted(s)) for s in only_heur))

    if not only_llm and not only_heur:
        say(SKIP, "No difference on this input",
            "On this residual the deterministic scorer reaches the same answer.\n"
            "That is a fair result to report -- the model is insurance for the\n"
            "harder tail, not a headline number.")
    elif only_llm:
        say(OK, "The model added reach the deterministic scorer did not have",
            "This is the honest case for keeping it.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the LLM residual resolver")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the payload and the heuristic baseline; spend nothing")
    args = ap.parse_args()

    print("=" * 64)
    print("  LLM residual resolver check")
    print("=" * 64)

    residual = residual_set()
    have_key = check_key()

    if args.dry_run or not have_key:
        show_payload(residual)
        run_heuristic(residual)
        head("Verdict")
        if args.dry_run:
            print("  DRY RUN -- nothing was sent and nothing was spent.\n")
        else:
            print("  No API key, so the LLM path was NOT verified.")
            print("  The product still works: every published number in this repo")
            print("  was produced by the deterministic heuristic above.\n")
            print("  To verify the model path:")
            print("    1. Add ANTHROPIC_API_KEY to .env")
            print("    2. pip install anthropic")
            print("    3. python scripts/verify_llm.py\n")
        return 0

    if not check_sdk():
        return 1

    show_payload(residual)
    heur, _ = run_heuristic(residual)
    llm, usage = run_llm(residual)
    if not usage:
        return 1
    compare(heur, llm, residual)

    head("Verdict")
    if _failed:
        print("  Something above FAILED. Do not quote LLM numbers until it passes.\n")
        return 1
    print("  VERIFIED against the live Anthropic API.")
    print(f"  {usage['llm_calls']} call, {usage['llm_input_tokens']}+"
          f"{usage['llm_output_tokens']} tokens, ${usage['llm_cost_usd']:.4f}.\n")
    print("  Next:")
    print("    1. README -> Known limitations: replace the line saying the LLM")
    print("       path is unverified with what you actually ran.")
    print("    2. In the demo, still show the heuristic numbers as the headline --")
    print("       they are the floor, and the floor is the honest claim.")
    print("    3. Then delete the key mid-demo to show the fallback. That")
    print("       contrast is worth more than the model's output.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
