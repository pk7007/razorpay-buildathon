"""Raw source dicts -> canonical ``Entry`` list.

Each source has its own quirks; the mapping is explicit and boring on purpose.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .models import Entry, Source

_UTR_RE = re.compile(r"\b(UTR\d{8,})\b", re.I)
_ORD_RE = re.compile(r"\b(ORD\d{5,})\b", re.I)


_EPOCH = date(1970, 1, 1)   # sentinel for an unparseable date -> lands in exceptions


def _to_date(value: str | int | float) -> date:
    try:
        if isinstance(value, (int, float)):
            return datetime.utcfromtimestamp(int(value)).date()
        s = str(value).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d %b %Y"):
            try:
                return datetime.strptime(s[: len(fmt) + 4].strip(), fmt).date()
            except ValueError:
                continue
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError, OSError, OverflowError):
        return _EPOCH


def _to_paise(value: str | int | float) -> int:
    try:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return round(value * 100)
        s = str(value).replace(",", "").replace("₹", "").strip()
        if s.lower() in ("", "-", "nan", "none", "null"):
            return 0
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()")
        # a decimal point means rupees; a bare integer is already paise (Razorpay API style)
        paise = round(float(s) * 100) if "." in s else int(s)
        return -paise if neg else paise
    except (ValueError, TypeError):
        return 0


def _clean_ref(*candidates: str | None) -> str | None:
    for c in candidates:
        if c and str(c).strip() not in ("", "-", "nan", "None"):
            return str(c).strip()
    return None


def _ref_from_narration(narration: str | None) -> str | None:
    if not narration:
        return None
    m = _UTR_RE.search(narration) or _ORD_RE.search(narration)
    return m.group(1).upper() if m else None


def normalize(source: Source, rows: list[dict]) -> list[Entry]:
    out: list[Entry] = []
    for i, r in enumerate(rows):
        r = {k: v for k, v in r.items()}  # copy
        if source == "payment":
            out.append(
                Entry(
                    id=_clean_ref(r.get("id")) or f"pay_{i}",
                    source=source,
                    amount_paise=_to_paise(r.get("amount", r.get("amount_paise", 0))),
                    value_date=_to_date(r.get("created_at") or r.get("date")),
                    reference=_clean_ref(r.get("order_id"), r.get("rrn"), r.get("id")),
                    narration=_clean_ref(r.get("method"), r.get("description"), r.get("status")),
                    raw=r,
                )
            )
        elif source == "settlement":
            out.append(
                Entry(
                    id=_clean_ref(r.get("id")) or f"setl_{i}",
                    source=source,
                    amount_paise=_to_paise(r.get("amount", 0)),
                    value_date=_to_date(
                        r.get("settled_at") or r.get("created_at") or r.get("date")
                    ),
                    reference=_clean_ref(r.get("utr"), r.get("id")),
                    narration="settlement payout",
                    fee_paise=_to_paise(r.get("fees", 0)),
                    tax_paise=_to_paise(r.get("tax", 0)),
                    raw=r,
                )
            )
        elif source == "bank":
            narration = _clean_ref(r.get("narration"), r.get("description"))
            amt = _to_paise(r.get("amount", 0))
            if str(r.get("type", "")).lower() in ("debit", "dr") and amt > 0:
                amt = -amt
            out.append(
                Entry(
                    id=_clean_ref(r.get("id")) or f"bank_{i}",
                    source=source,
                    amount_paise=amt,
                    value_date=_to_date(r.get("value_date") or r.get("date")),
                    reference=_clean_ref(r.get("utr"), r.get("ref"))
                    or _ref_from_narration(narration),
                    narration=narration,
                    raw=r,
                )
            )
        elif source == "ledger":
            out.append(
                Entry(
                    id=_clean_ref(r.get("id")) or f"ldgr_{i}",
                    source=source,
                    amount_paise=_to_paise(r.get("amount", 0)),
                    value_date=_to_date(r.get("date")),
                    reference=_clean_ref(r.get("external_ref"), r.get("ref"))
                    or _ref_from_narration(r.get("memo")),
                    narration=_clean_ref(r.get("memo"), r.get("description")),
                    raw=r,
                )
            )
        else:  # pragma: no cover
            raise ValueError(f"unknown source {source}")
    return out
