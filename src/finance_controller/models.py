"""Canonical data model.

Money is always integer **paise**. Dates are IST calendar dates (``datetime.date``).
Every downstream stage speaks this vocabulary and nothing else.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["payment", "settlement", "bank", "ledger"]

ExceptionCategory = Literal[
    "missing_in_bank",       # booked in ledger, never hit the bank  -> recoverable revenue
    "missing_in_ledger",     # money in the bank, never booked        -> unrecorded income
    "payout_in_transit",     # settlement raised, bank credit not yet -> resolves next cycle
    "fee_mismatch",          # amounts differ by ~ gateway fee + GST
    "split_settlement",      # many payments -> one payout, not auto-grouped
    "merged_payout",         # one bank credit = many settlements
    "duplicate",             # same source, amount and date twice
    "fx_or_adjustment",      # residual difference looks like FX / manual adjustment
    "amount_mismatch",       # candidate exists but amounts do not reconcile
    "unknown",               # no structural signal
]

Stage = Literal["deterministic", "structural", "agent", "heuristic"]

GroupStatus = Literal[
    "complete",             # payment + settlement + bank + ledger all tied
    "awaiting_settlement",  # recent sale, settlement not raised yet
    "awaiting_payout",      # settlement raised, bank credit due within the payout cycle
    "payout_overdue",       # settlement raised, payout should have landed -> recoverable
    "unbooked_payout",      # settled to bank but never recorded in the ledger
    "partial",              # some legs tied, others missing with no clean reason
]


class Entry(BaseModel):
    """One normalized row from any source."""

    id: str
    source: Source
    amount_paise: int
    value_date: date
    reference: str | None = None      # utr / rrn / order_id / external_ref (cleaned)
    narration: str | None = None
    fee_paise: int = 0                # settlements only
    tax_paise: int = 0               # settlements only (GST on fee)
    raw: dict = Field(default_factory=dict, repr=False)

    @property
    def net_paise(self) -> int:
        """Amount after gateway fee + tax (identity for non-settlement rows)."""
        return self.amount_paise - self.fee_paise - self.tax_paise


class MatchGroup(BaseModel):
    group_id: str
    entry_ids: list[str]
    stage: Stage
    rule: str
    confidence: float
    rationale: str
    amount_paise: int = 0            # representative (gross) sale amount of the group
    status: GroupStatus = "complete"
    sources: list[Source] = Field(default_factory=list)


class ReconException(BaseModel):
    entry_id: str
    source: Source
    amount_paise: int
    value_date: date
    category: ExceptionCategory
    confidence: float
    suggested_action: str
    rationale: str


class AuditRecord(BaseModel):
    seq: int
    ts: datetime
    stage: Stage
    rule: str
    inputs: list[str]
    outcome: Literal["matched", "exception", "rejected"]
    confidence: float
    rationale: str


class MoneySummary(BaseModel):
    currency: str = "INR"
    entries_total: int
    gross_processed_paise: int       # total captured payment volume
    reconciled_paise: int            # gross of fully-tied (complete) groups
    in_transit_paise: int            # settled/awaiting payout within the normal cycle
    recoverable_paise: int           # payout overdue or never booked -> chase this
    unrecorded_paise: int            # bank credits with no ledger entry
    in_exception_paise: int          # total rupee value sitting in exceptions


class RunMetrics(BaseModel):
    total_entries: int
    groups: int
    matched_entries: int
    exceptions: int
    auto_match_rate: float
    deterministic_share: float
    structural_share: float
    resolver_share: float
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    exception_category_accuracy: float | None = None
    replay_stable: bool = True
    latency_ms: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0


class ReconResult(BaseModel):
    dataset: str
    generated_at: datetime
    entries: list[Entry]
    groups: list[MatchGroup]
    exceptions: list[ReconException]
    audit: list[AuditRecord]
    money: MoneySummary
    metrics: RunMetrics
    resolver_mode: Literal["llm", "heuristic"]
