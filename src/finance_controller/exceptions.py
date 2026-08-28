"""Turn every leftover entry into an explained exception. No silent drops."""
from __future__ import annotations

from .models import Entry, ReconException

_ACTION = {
    "missing_in_bank": "confirm payout landed; check UTR with bank",
    "missing_in_ledger": "post the journal entry in books",
    "fee_mismatch": "reconcile gateway fee + GST against settlement report",
    "timing_gap": "expected to clear next cycle; re-run after settlement date",
    "split_settlement": "group child payouts under the parent settlement id",
    "merged_payout": "de-aggregate the bank credit against payment list",
    "duplicate": "verify not double-counted; void one entry",
    "fx_or_adjustment": "book the FX / adjustment difference",
    "unknown": "manual review",
}


def categorize(entry: Entry) -> ReconException:
    src = entry.source
    narr = (entry.narration or "").lower()

    if src == "payment":
        cat = "missing_in_bank"
        conf = 0.6
    elif src == "bank":
        cat = "missing_in_ledger"
        conf = 0.55
    elif src == "settlement" and ("fee" in narr or "tax" in narr):
        cat = "fee_mismatch"
        conf = 0.5
    elif src == "ledger":
        cat = "missing_in_bank"
        conf = 0.5
    else:
        cat = "unknown"
        conf = 0.3

    return ReconException(
        entry_id=entry.id,
        category=cat,  # type: ignore[arg-type]
        confidence=conf,
        suggested_action=_ACTION[cat],
    )


def build_exceptions(leftover: list[Entry]) -> list[ReconException]:
    return [categorize(e) for e in leftover]
