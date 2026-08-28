"""Stage 1: deterministic matching. No guessing — ambiguity goes to the residual set."""
from __future__ import annotations

from collections import defaultdict

from .audit import AuditLog
from .config import SETTINGS
from .models import Entry, MatchGroup

RULE_EXACT = "exact-ref-match@v1"
RULE_TOLERANT = "tolerant-amount-date-match@v1"


def _group_id(n: int) -> str:
    return f"g{n:05d}"


def deterministic_match(
    entries: list[Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[Entry]]:
    """Return (match_groups, residual_entries)."""
    by_id = {e.id: e for e in entries}
    consumed: set[str] = set()
    groups: list[MatchGroup] = []
    counter = 0

    # ---- Stage 1a: exact reference match -------------------------------------
    by_ref: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.reference:
            by_ref[e.reference.strip().lower()].append(e)

    for ref, bucket in by_ref.items():
        if len(bucket) < 2:
            continue
        amounts = {e.amount_paise for e in bucket}
        if max(amounts) - min(amounts) > SETTINGS.amount_tolerance_paise:
            continue  # same ref, incompatible amounts -> let Stage 2 look
        counter += 1
        ids = [e.id for e in bucket]
        groups.append(
            MatchGroup(
                group_id=_group_id(counter),
                entry_ids=ids,
                stage="deterministic",
                rule=RULE_EXACT,
                confidence=1.0,
                rationale=f"shared reference {ref!r}, amounts within tolerance",
            )
        )
        consumed.update(ids)
        audit.record(
            stage="deterministic",
            rule=RULE_EXACT,
            inputs=ids,
            outcome="matched",
            confidence=1.0,
            rationale=f"shared reference {ref!r}",
        )

    # ---- Stage 1b: tolerant amount+date, unique candidate each side ----------
    remaining = [e for e in entries if e.id not in consumed]
    by_source: dict[str, list[Entry]] = defaultdict(list)
    for e in remaining:
        by_source[e.source].append(e)

    # match payments <-> bank as the common case; extend as needed
    left = by_source.get("payment", [])
    right = by_source.get("bank", [])
    for le in left:
        if le.id in consumed:
            continue
        cands = [
            re
            for re in right
            if re.id not in consumed
            and abs(re.amount_paise - le.amount_paise) <= SETTINGS.amount_tolerance_paise
            and abs((re.value_date - le.value_date).days) <= SETTINGS.date_tolerance_days
        ]
        if len(cands) == 1:
            re = cands[0]
            counter += 1
            ids = [le.id, re.id]
            groups.append(
                MatchGroup(
                    group_id=_group_id(counter),
                    entry_ids=ids,
                    stage="deterministic",
                    rule=RULE_TOLERANT,
                    confidence=0.95,
                    rationale="unique candidate within amount/date tolerance",
                )
            )
            consumed.update(ids)
            audit.record(
                stage="deterministic",
                rule=RULE_TOLERANT,
                inputs=ids,
                outcome="matched",
                confidence=0.95,
                rationale="unique candidate within amount/date tolerance",
            )

    residual = [by_id[i] for i in by_id if i not in consumed]
    return groups, residual
