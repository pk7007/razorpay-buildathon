"""Turn every unresolved entry into an explained exception. No silent drops.

Also pulls duplicate rows out of otherwise-good groups.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass

from .audit import AuditLog
from .config import SETTINGS
from .models import Entry, ExceptionCategory, MatchGroup, ReconException
from .money import fmt

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
    "orphan_refund": "Locate the original payment — this refund names one that is "
                     "not in the batch. It may be in an earlier period.",
    "orphan_chargeback": "Locate the disputed payment — it is not in this batch, "
                         "likely from an earlier period.",
    "over_refunded": "Refunds exceed the payment they reduce — a data error. Verify "
                     "the refund records before settling.",
    "currency_mismatch": "A counterpart exists in a different currency — confirm the "
                         "FX rate and book the conversion explicitly.",
    "unknown": "Manual review — no structural match found.",
}


def extract_duplicates(
    groups: list[MatchGroup], by_id: dict[str, Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[ReconException]]:
    """Within each group, keep one entry per (source, amount); the rest are duplicates.

    A ledger or payment row repeated inside one group is a double-booking. A
    *bank* or *settlement* row repeated is stronger still -- it means the group
    claims one payout arrived twice -- but only when the two rows also share a
    value date. Two credits of the same size on different days inside one group
    are a legitimate merged payout, which is a different rule's business.
    """
    kept_groups: list[MatchGroup] = []
    dups: list[ReconException] = []
    for g in groups:
        seen: dict[tuple[str, int], str] = {}
        seen_dates: dict[tuple[str, int], object] = {}
        keep: list[str] = []
        for eid in g.entry_ids:
            e = by_id[eid]
            key = (e.source, round(e.amount_paise, -1))
            same_day = seen_dates.get(key) == e.value_date
            is_duplicate = key in seen and (
                e.source in ("ledger", "payment")
                or (e.source in ("bank", "settlement") and same_day)
            )
            if is_duplicate:
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
                seen_dates[key] = e.value_date
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

    # indices so categorising one entry is a lookup, not a scan of every entry:
    # sorted amounts per source for "is there anything near this amount", and a
    # settlement-by-reference map for the shared-UTR check
    idx = _Indices(
        amounts={src: sorted(x.amount_paise for x in rows) for src, rows in by_source.items()},
        settlement_by_ref={
            s.reference.lower(): s for s in by_source["settlement"] if s.reference
        },
    )

    for e in residual:
        cat, conf, why = _categorize(e, idx, tol)
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


@dataclass
class _Indices:
    amounts: dict[str, list[int]]
    settlement_by_ref: dict[str, Entry]

    def has_amount_near(self, source: str, amt: int, tol: int) -> bool:
        """Binary search instead of scanning every row of that source."""
        col = self.amounts.get(source)
        if not col:
            return False
        i = bisect_left(col, amt - tol)
        return i < len(col) and col[i] <= amt + tol


def _categorize(
    e: Entry, idx: _Indices, tol: int
) -> tuple[ExceptionCategory, float, str]:
    amt = e.amount_paise

    # a deduction that reached this point never found its payment
    if e.source in ("refund", "chargeback"):
        cat: ExceptionCategory = (
            "orphan_refund" if e.source == "refund" else "orphan_chargeback"
        )
        named = f" naming payment {e.related_reference}" if e.related_reference else ""
        return (
            cat, 0.85,
            f"{e.source} of {fmt(amt, e.currency)} on {e.value_date}{named}, but no "
            f"such payment is in this batch — it is most likely in an earlier period",
        )

    if e.source == "bank" and amt < 0:
        return (
            "fee_mismatch", 0.6,
            f"bank debit {fmt(-amt, e.currency)} on {e.value_date} — money left the "
            f"account with no settlement or ledger entry explaining it. Typically a "
            f"bank charge, a reversal, or a transfer booked elsewhere",
        )

    if e.source == "bank" and amt > 0:
        # same UTR as a settlement but amount short -> a deduction happened
        if e.reference:
            s = idx.settlement_by_ref.get(e.reference.lower())
            if s is not None:
                diff = s.amount_paise - amt
                return (
                    "fee_mismatch", 0.8,
                    f"settlement {s.id} shares UTR {e.reference} but credit is "
                    f"₹{diff/100:,.2f} short — likely a bank / processing charge",
                )
        # no settlement, no ledger entry near the amount -> unrecorded income
        if not idx.has_amount_near("ledger", amt, tol):
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
        if not idx.has_amount_near("bank", amt, tol):
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

    return (
        "unknown", 0.3,
        f"{e.source} entry {fmt(amt, e.currency)} on {e.value_date}: no shared "
        f"reference, and no counterpart of a comparable amount in any other source",
    )
