"""Canonical data model.

Money is always integer **paise**. Dates are IST calendar dates (``datetime.date``).
Every downstream stage speaks this vocabulary and nothing else.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .money import DEFAULT_CURRENCY

Source = Literal[
    "payment",      # gateway capture (gross)
    "settlement",   # payout to the bank (net of fee/tax/TDS/deductions)
    "bank",         # what actually hit the current account
    "ledger",       # what the books say
    "refund",       # money returned to a customer, full or partial
    "chargeback",   # a disputed payment clawed back by the network
]

# sources that reduce what a merchant is owed. They are NOT unmatched noise --
# they are financial events with their own lifecycle, and the settlement
# equation subtracts them explicitly.
DEDUCTION_SOURCES: tuple[str, ...] = ("refund", "chargeback")

DisputeStatus = Literal["open", "under_review", "won", "lost", "accepted"]

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
    "orphan_refund",         # a refund with no payment to attach it to
    "orphan_chargeback",     # a chargeback with no payment to attach it to
    "over_refunded",         # refunds exceed the payment they claim to reduce
    "currency_mismatch",     # a counterpart exists but in another currency
    "unknown",               # no structural signal
]

Stage = Literal["deterministic", "structural", "agent", "heuristic"]

GroupStatus = Literal[
    "complete",             # payment + settlement + bank + ledger all tied
    "awaiting_settlement",  # recent sale, settlement not raised yet
    "awaiting_payout",      # settlement raised, bank credit due within the payout cycle
    "payout_overdue",       # settlement raised, payout should have landed -> recoverable
    "unbooked_payout",      # settled to bank but never recorded in the ledger
    "fully_refunded",       # refunds/chargebacks cancelled it out; no payout is due
    "ambiguous_split",      # part of a batch payout that could not be attributed uniquely
    "partial",              # some legs tied, others missing with no clean reason
]


class Entry(BaseModel):
    """One normalized row from any source.

    ``amount_paise`` is in the *minor units of ``currency``* -- paise for INR,
    cents for USD. The field keeps its historical name so existing call sites
    and stored results stay valid.
    """

    id: str
    source: Source
    amount_paise: int
    value_date: date
    currency: str = DEFAULT_CURRENCY
    reference: str | None = None      # utr / rrn / order_id / external_ref (cleaned)
    narration: str | None = None
    method: str | None = None         # upi / card / netbanking -- picks the fee rule

    # settlement components. None means "the source did not report this", which
    # is different from zero and drives the actual-vs-estimated distinction.
    fee_paise: int = 0
    tax_paise: int = 0
    tds_paise: int | None = None
    fee_reported: bool = False        # True when fee/tax came from the source

    # deduction linkage: a refund or chargeback points at the payment it reduces
    related_reference: str | None = None
    dispute_status: DisputeStatus | None = None

    raw: dict = Field(default_factory=dict, repr=False)

    @property
    def net_paise(self) -> int:
        """Amount after fee + tax + TDS (identity for non-settlement rows)."""
        return self.amount_paise - self.fee_paise - self.tax_paise - (self.tds_paise or 0)

    @property
    def is_deduction(self) -> bool:
        return self.source in DEDUCTION_SOURCES


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
    ambiguous_paise: int = 0         # paid out, but not uniquely attributable to a batch
    in_exception_paise: int = 0      # total rupee value sitting in exceptions


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
