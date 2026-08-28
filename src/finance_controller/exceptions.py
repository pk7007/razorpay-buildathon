"""Turn every unresolved entry into an explained exception. No silent drops.

Also pulls duplicate rows out of otherwise-good groups.
"""
from __future__ import annotations

from collections import defaultdict

from .audit import AuditLog
from .config import SETTINGS
from .models import Entry, ExceptionCategory, MatchGroup, ReconException

_ACTION: dict[ExceptionCategory, str] = {
    "missing_in_bank": "Chase the payout — money is booked but never reached the bank.",
    "missing_in_ledger": "Book the journal entry — bank received funds with no matching record.",
    "payout_in_transit": "No action — settlement raised, bank credit expected next cycle.",
    "fee_mismatch": "Reconcile the deduction against the settlement / bank charge schedule.",
    "split_settlement": "Group the child payouts under the parent settlement.",
    "merged_payout": "De-aggregate the bank credit against the settlement list.",
    "duplicate": "Void one entry — the same amount is recorded twice.",
    "fx_or_adjustment": "Book the FX / manual adjustment difference.",
    "amount_mismatch": "A counterpart exists but the amounts do not reconcile — investigate.",
    "unknown": "Manual review — no structural match found.",
}


def extract_duplicates(
    groups: list[MatchGroup], by_id: dict[str, Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[ReconException]]:
    """Within each group, keep one entry per (source, amount); the rest are duplicates."""
    kept_groups: list[MatchGroup] = []
    dups: list[ReconException] = []
    for g in groups:
        seen: dict[tuple[str, int], str] = {}
        keep: list[str] = []
        for eid in g.entry_ids:
            e = by_id[eid]
            key = (e.source, round(e.amount_paise, -1))
            if key in seen and e.source in ("ledger", "payment"):
                dups.append(
                    ReconException(
                        entry_id=eid,
                        source=e.source,
                        amount_paise=e.amount_paise,
                        value_date=e.value_date,
                        category="duplicate",
                        confidence=0.9,
                        suggested_action=_ACTION["duplicate"],
                        rationale=(
                            f"same {e.source} amount ₹{e.amount_paise/100:,.2f} as {seen[key]} "
                            f"in group {g.group_id}"
                        ),
                    )
                )
                audit.record(
                    stage="structural", rule="duplicate-in-group@v1", inputs=[eid, seen[key]],
                    outcome="exception", confidence=0.9,
                    rationale=f"duplicate {e.source} row in {g.group_id}",
                )
            else:
                seen[key] = eid
                keep.append(eid)
        if len(keep) != len(g.entry_ids):
            g = g.model_copy(update={"entry_ids": keep})
        kept_groups.append(g)
    return kept_groups, dups


def classify_residual(
    residual: list[Entry], all_entries: list[Entry], audit: AuditLog
) -> list[ReconException]:
    by_source: dict[str, list[Entry]] = defaultdict(list)
    for e in all_entries:
        by_source[e.source].append(e)
    tol = SETTINGS.amount_tolerance_paise
    out: list[ReconException] = []

    for e in residual:
        cat, conf, why = _categorize(e, by_source, tol)
        out.append(
            ReconException(
                entry_id=e.id,
                source=e.source,
                amount_paise=e.amount_paise,
                value_date=e.value_date,
                category=cat,
                confidence=conf,
                suggested_action=_ACTION[cat],
                rationale=why,
            )
        )
        audit.record(
            stage="structural", rule="classify-residual@v1", inputs=[e.id],
            outcome="exception", confidence=conf, rationale=f"{cat}: {why}",
        )
    return out


def _categorize(
    e: Entry, by_source: dict[str, list[Entry]], tol: int
) -> tuple[ExceptionCategory, float, str]:
    amt = e.amount_paise

    if e.source == "bank" and amt > 0:
        # same UTR as a settlement but amount short -> a deduction happened
        if e.reference:
            for s in by_source["settlement"]:
                if s.reference and s.reference.lower() == e.reference.lower():
                    diff = s.amount_paise - amt
                    return (
                        "fee_mismatch", 0.8,
                        f"settlement {s.id} shares UTR {e.reference} but credit is "
                        f"₹{diff/100:,.2f} short — likely a bank / processing charge",
                    )
        # no settlement, no ledger entry near the amount -> unrecorded income
        near_ledger = any(abs(x.amount_paise - amt) <= tol for x in by_source["ledger"])
        if not near_ledger:
            return (
                "missing_in_ledger", 0.75,
                f"bank credit ₹{amt/100:,.2f} on {e.value_date} with no settlement and "
                f"no ledger entry of a comparable amount",
            )
        return (
            "unknown", 0.4,
            f"bank credit ₹{amt/100:,.2f} could not be tied to a settlement or payment",
        )

    if e.source == "ledger":
        near_bank = any(abs(x.amount_paise - amt) <= tol for x in by_source["bank"])
        if not near_bank:
            return (
                "missing_in_bank", 0.7,
                f"ledger income ₹{amt/100:,.2f} on {e.value_date} with no bank credit of a "
                f"comparable amount — potential unrecovered revenue",
            )
        return ("amount_mismatch", 0.45,
                f"ledger entry ₹{amt/100:,.2f} has a near-amount bank line but no clean match")

    if e.source == "payment":
        return (
            "missing_in_bank", 0.6,
            f"captured payment ₹{amt/100:,.2f} with no settlement / payout tied to it",
        )

    if e.source == "settlement":
        return (
            "payout_in_transit", 0.6,
            f"settlement ₹{amt/100:,.2f} settled {e.value_date} with no bank credit yet — "
            f"expected to clear within the payout cycle",
        )

    return ("unknown", 0.3, "no structural signal")
