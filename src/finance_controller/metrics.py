"""Scoring: pair-based precision/recall + money summary + replay stability."""
from __future__ import annotations

import hashlib
import json
from itertools import combinations

from .models import (
    Entry,
    MatchGroup,
    MoneySummary,
    ReconException,
    RunMetrics,
)


def _pairs(groups: list[list[str]]) -> set[frozenset[str]]:
    out: set[frozenset[str]] = set()
    for g in groups:
        for a, b in combinations(sorted(g), 2):
            out.add(frozenset((a, b)))
    return out


def score_matches(
    groups: list[MatchGroup],
    labels: dict[str, list[str]] | None,
    total_entries: int,
    *,
    latency_ms: int,
    usage: dict,
) -> RunMetrics:
    matched = {i for g in groups for i in g.entry_ids}
    by_stage = {"deterministic": 0, "structural": 0, "resolver": 0}
    for g in groups:
        if g.stage == "deterministic":
            by_stage["deterministic"] += 1
        elif g.stage == "structural":
            by_stage["structural"] += 1
        else:
            by_stage["resolver"] += 1
    ng = len(groups) or 1

    m = RunMetrics(
        total_entries=total_entries,
        groups=len(groups),
        matched_entries=len(matched),
        exceptions=total_entries - len(matched),
        auto_match_rate=round(len(matched) / total_entries, 4) if total_entries else 0.0,
        deterministic_share=round(by_stage["deterministic"] / ng, 4),
        structural_share=round(by_stage["structural"] / ng, 4),
        resolver_share=round(by_stage["resolver"] / ng, 4),
        latency_ms=latency_ms,
        llm_calls=usage.get("llm_calls", 0),
        llm_input_tokens=usage.get("llm_input_tokens", 0),
        llm_output_tokens=usage.get("llm_output_tokens", 0),
        llm_cost_usd=usage.get("llm_cost_usd", 0.0),
    )

    if labels:
        pred = _pairs([g.entry_ids for g in groups])
        true = _pairs(list(labels.values()))
        tp = len(pred & true)
        m.precision = round(tp / len(pred), 4) if pred else 1.0
        m.recall = round(tp / len(true), 4) if true else 1.0
        denom = (m.precision + m.recall) or 1.0
        m.f1 = round(2 * m.precision * m.recall / denom, 4)
    return m


def score_exceptions(
    exceptions: list[ReconException], truth: dict[str, str] | None
) -> float | None:
    if not truth:
        return None
    got = {e.entry_id: e.category for e in exceptions}
    hits = sum(1 for eid, cat in truth.items() if got.get(eid) == cat)
    return round(hits / len(truth), 4)


def classify_group(
    g: MatchGroup,
    by_id: dict[str, Entry],
    dataset_end,
    settlement_lag: int,
    ambiguous_ids: set[str] | None = None,
) -> None:
    """Set ``g.status`` and ``g.sources`` from which legs are present and how recent."""
    members = [by_id[i] for i in g.entry_ids if i in by_id]
    srcs = {m.source for m in members}
    g.sources = sorted(srcs)  # type: ignore[assignment]
    sale = _group_sale_paise(members)
    g.amount_paise = sale

    has_book = bool({"payment", "ledger"} & srcs)
    latest = max((m.value_date for m in members), default=dataset_end)
    days_old = (dataset_end - latest).days

    # Refunds and chargebacks can cancel a sale out entirely. Nothing is owed, so
    # nothing is overdue -- reporting these as an outstanding payout would send
    # someone chasing money that was never going to arrive.
    gross = sum(m.amount_paise for m in members if m.source == "payment") or sum(
        m.amount_paise for m in members if m.source == "ledger"
    )
    deducted = sum(m.amount_paise for m in members if m.source in ("refund", "chargeback"))
    if has_book and "bank" not in srcs and deducted >= gross > 0:
        g.status = "fully_refunded"
        return

    if "bank" in srcs and has_book:
        g.status = "complete"
    elif "bank" in srcs and not has_book:
        g.status = "unbooked_payout"
    elif "settlement" in srcs and has_book:
        g.status = "awaiting_payout" if days_old <= settlement_lag + 1 else "payout_overdue"
    elif ambiguous_ids and any(i in ambiguous_ids for i in g.entry_ids):
        # the payout exists; we just could not prove WHICH batch it belongs to.
        # calling this "overdue" would send someone chasing money that arrived.
        g.status = "ambiguous_split"
    elif has_book:  # payment / ledger only
        g.status = "awaiting_settlement" if days_old <= settlement_lag + 2 else "payout_overdue"
    else:
        g.status = "partial"


def _group_sale_paise(members: list[Entry]) -> int:
    pay = sum(m.amount_paise for m in members if m.source == "payment")
    ldg = sum(m.amount_paise for m in members if m.source == "ledger")
    setl = sum(
        m.amount_paise + m.fee_paise + m.tax_paise for m in members if m.source == "settlement"
    )
    bank = sum(m.amount_paise for m in members if m.source == "bank" and m.amount_paise > 0)
    return pay or ldg or setl or bank or max((m.amount_paise for m in members), default=0)


def money_summary(
    entries: list[Entry],
    groups: list[MatchGroup],
    exceptions: list[ReconException],
) -> MoneySummary:
    gross = sum(e.amount_paise for e in entries if e.source == "payment")

    def total(*statuses: str) -> int:
        return sum(g.amount_paise for g in groups if g.status in statuses)

    # a cancelled sale is neither reconciled money nor money to chase

    return MoneySummary(
        entries_total=len(entries),
        gross_processed_paise=gross,
        reconciled_paise=total("complete"),
        in_transit_paise=total("awaiting_settlement", "awaiting_payout"),
        # Money owed to the merchant that has not arrived. A bank DEBIT is the
        # opposite of that -- a ₹118 charge leaving the account is not revenue
        # to chase, and letting its negative amount in here quietly reduced the
        # figure a controller uses to decide what to go and collect.
        recoverable_paise=total("payout_overdue", "partial")
        + sum(
            e.amount_paise for e in exceptions
            if e.category in ("missing_in_bank", "fee_mismatch") and e.amount_paise > 0
        ),
        unrecorded_paise=total("unbooked_payout")
        + sum(e.amount_paise for e in exceptions if e.category == "missing_in_ledger"),
        ambiguous_paise=total("ambiguous_split"),
        in_exception_paise=sum(e.amount_paise for e in exceptions),
    )


def result_fingerprint(groups: list[MatchGroup], exceptions: list[ReconException]) -> str:
    payload = {
        "groups": sorted(sorted(g.entry_ids) for g in groups),
        "exceptions": sorted((e.entry_id, e.category) for e in exceptions),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
