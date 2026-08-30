"""Ingestion validation.

A reconciliation run is only as trustworthy as the rows that went into it, so
every upload is inspected before it is matched and the findings are reported
rather than silently absorbed.

The governing rule is **partial acceptance**: one malformed row out of 4,000
must not fail the batch, and it must not vanish either. Valid rows reconcile,
invalid rows are quarantined with the reason and the row number, and the caller
gets both numbers. A finance user can then fix twelve rows instead of guessing
why a total is off by an amount they cannot find.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .models import Source
from .money import is_supported, normalize_code
from .normalize import _to_date, _to_paise

_EPOCH_SENTINEL = "1970-01-01"   # what _to_date returns when it cannot parse


@dataclass
class RowIssue:
    row: int                     # 1-based, matching what a spreadsheet shows
    field: str
    problem: str
    severity: str = "error"      # error -> quarantined, warning -> still used


@dataclass
class QualityReport:
    source: Source
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    empty_rows: int = 0
    issues: list[RowIssue] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.invalid_rows == 0

    def summary(self) -> dict:
        by_problem = Counter(i.problem for i in self.issues)
        return {
            "source": self.source,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "duplicate_rows": self.duplicate_rows,
            "empty_rows": self.empty_rows,
            "currencies": self.currencies,
            "issues_by_problem": dict(by_problem.most_common()),
            # a handful is enough to act on; the rest would just be noise
            "sample_issues": [
                {"row": i.row, "field": i.field, "problem": i.problem, "severity": i.severity}
                for i in self.issues[:25]
            ],
        }


def validate(source: Source, rows: list[dict]) -> tuple[list[dict], QualityReport]:
    """Split rows into (accepted, report). Never raises on bad data."""
    rep = QualityReport(source=source, total_rows=len(rows))
    accepted: list[dict] = []
    seen_ids: dict[str, int] = {}
    seen_sigs: dict[tuple, int] = {}
    currencies: set[str] = set()

    for n, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            rep.issues.append(RowIssue(n, "row", "not a record"))
            rep.invalid_rows += 1
            continue

        row = {k: v for k, v in raw.items() if not str(k).startswith("_")}
        if not any(str(v).strip() for v in row.values() if v is not None):
            rep.empty_rows += 1
            continue

        problems: list[RowIssue] = []

        # --- amount -------------------------------------------------------
        amt_raw = row.get("amount", row.get("amount_paise"))
        if amt_raw in (None, ""):
            if source == "bank" and (row.get("debit") or row.get("credit")):
                amt = _to_paise(row.get("credit") or row.get("debit"))
            else:
                problems.append(RowIssue(n, "amount", "missing amount"))
                amt = 0
        else:
            amt = _to_paise(amt_raw)
            if amt == 0 and str(amt_raw).strip() not in ("0", "0.0", "0.00"):
                problems.append(RowIssue(n, "amount", f"unparseable amount {amt_raw!r}"))
        if abs(amt) > 10**13:      # > 100 crore in paise: a data error, not a sale
            problems.append(RowIssue(n, "amount", "implausibly large amount"))
        if amt < 0 and source in ("payment", "settlement", "refund", "chargeback"):
            problems.append(
                RowIssue(n, "amount", f"negative amount on a {source}", "warning")
            )

        # --- date ---------------------------------------------------------
        date_raw = (
            row.get("date") or row.get("value_date") or row.get("created_at")
            or row.get("settled_at")
        )
        if date_raw in (None, ""):
            problems.append(RowIssue(n, "date", "missing date"))
        elif _to_date(date_raw).isoformat() == _EPOCH_SENTINEL:
            problems.append(RowIssue(n, "date", f"unparseable date {date_raw!r}"))

        # --- currency -----------------------------------------------------
        cur_raw = row.get("currency")
        code = str(cur_raw).strip().upper() if cur_raw else ""
        if code and code != "INR" and not is_supported(code):
            problems.append(
                RowIssue(n, "currency", f"unsupported currency {code!r}, treated as INR",
                         "warning")
            )
        currencies.add(normalize_code(cur_raw))

        # --- identity + duplicates ----------------------------------------
        rid = str(row.get("id") or "").strip()
        if rid:
            if rid in seen_ids:
                problems.append(
                    RowIssue(n, "id", f"duplicate id {rid!r} (first seen at row {seen_ids[rid]})")
                )
            else:
                seen_ids[rid] = n
        else:
            problems.append(RowIssue(n, "id", "missing id, one will be generated", "warning"))

        # a repeated (amount, date, reference) with no id is a re-exported row
        sig = (amt, str(date_raw), str(row.get("utr") or row.get("reference")
                                        or row.get("order_id") or ""))
        if not rid and sig[2]:
            if sig in seen_sigs:
                problems.append(
                    RowIssue(n, "row", f"identical to row {seen_sigs[sig]}", "warning")
                )
                rep.duplicate_rows += 1
            else:
                seen_sigs[sig] = n

        # --- deduction linkage --------------------------------------------
        if source in ("refund", "chargeback"):
            if not (row.get("payment_id") or row.get("order_id")
                    or row.get("related_reference")):
                problems.append(
                    RowIssue(n, "payment_id",
                             f"{source} does not name the payment it reduces", "warning")
                )

        rep.issues.extend(problems)
        if any(p.severity == "error" for p in problems):
            rep.invalid_rows += 1
            if any(p.problem.startswith("duplicate id") for p in problems):
                rep.duplicate_rows += 1
            continue

        accepted.append(raw)
        rep.valid_rows += 1

    rep.currencies = sorted(currencies)
    return accepted, rep


def combined_summary(reports: dict[str, QualityReport]) -> dict:
    """Roll several per-source reports into one figure for the UI."""
    total = sum(r.total_rows for r in reports.values())
    valid = sum(r.valid_rows for r in reports.values())
    invalid = sum(r.invalid_rows for r in reports.values())
    dupes = sum(r.duplicate_rows for r in reports.values())
    empty = sum(r.empty_rows for r in reports.values())
    currencies = sorted({c for r in reports.values() for c in r.currencies})
    return {
        "total_rows": total,
        "valid_rows": valid,
        "invalid_rows": invalid,
        "duplicate_rows": dupes,
        "empty_rows": empty,
        "currencies": currencies,
        "accepted_pct": round(valid / total, 4) if total else 1.0,
        "per_source": {k: r.summary() for k, r in reports.items() if r.total_rows},
    }
