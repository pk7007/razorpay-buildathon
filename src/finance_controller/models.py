"""Canonical data model. Amounts are integer paise; dates are IST calendar dates."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["payment", "settlement", "bank", "ledger"]

ExceptionCategory = Literal[
    "timing_gap",
    "fee_mismatch",
    "split_settlement",
    "merged_payout",
    "missing_in_bank",
    "missing_in_ledger",
    "duplicate",
    "fx_or_adjustment",
    "unknown",
]


class Entry(BaseModel):
    """One normalized row from any source."""

    id: str
    source: Source
    amount_paise: int
    value_date: date
    reference: str | None = None  # utr / order_id / external_ref
    narration: str | None = None
    raw: dict = Field(default_factory=dict, repr=False)


class MatchGroup(BaseModel):
    group_id: str
    entry_ids: list[str]
    stage: Literal["deterministic", "agent"]
    rule: str
    confidence: float
    rationale: str


class ReconException(BaseModel):
    entry_id: str
    category: ExceptionCategory
    confidence: float
    suggested_action: str


class AuditRecord(BaseModel):
    ts: datetime
    stage: Literal["deterministic", "agent"]
    rule: str
    inputs: list[str]
    outcome: Literal["matched", "exception"]
    confidence: float
    rationale: str
