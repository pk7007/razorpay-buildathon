"""Raw source dicts -> canonical Entry list.

Each source has its own quirks; keep the mapping explicit and boring.
"""
from __future__ import annotations

from datetime import date, datetime

from .models import Entry, Source


def _to_date(value: str | int | float) -> date:
    if isinstance(value, (int, float)):  # epoch seconds (Razorpay style)
        return datetime.utcfromtimestamp(int(value)).date()
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {value!r}")


def _to_paise(value: str | int | float) -> int:
    if isinstance(value, int):
        return value
    # accept "1234.56" (rupees) or "123456" (paise, no dot)
    s = str(value).replace(",", "").strip()
    if "." in s:
        return round(float(s) * 100)
    return int(s)


def normalize(source: Source, rows: list[dict]) -> list[Entry]:
    out: list[Entry] = []
    for i, r in enumerate(rows):
        if source == "payment":
            out.append(
                Entry(
                    id=r.get("id") or f"pay_{i}",
                    source=source,
                    amount_paise=_to_paise(r.get("amount", r.get("amount_paise", 0))),
                    value_date=_to_date(r.get("created_at") or r.get("date")),
                    reference=r.get("order_id") or r.get("id"),
                    narration=r.get("description") or r.get("method"),
                    raw=r,
                )
            )
        elif source == "settlement":
            out.append(
                Entry(
                    id=r.get("id") or f"setl_{i}",
                    source=source,
                    amount_paise=_to_paise(r.get("amount", 0)),
                    value_date=_to_date(r.get("settled_at") or r.get("created_at") or r.get("date")),
                    reference=r.get("utr") or r.get("id"),
                    narration=f"fees={r.get('fees')} tax={r.get('tax')}",
                    raw=r,
                )
            )
        elif source == "bank":
            out.append(
                Entry(
                    id=r.get("id") or f"bank_{i}",
                    source=source,
                    amount_paise=_to_paise(r.get("amount", 0)),
                    value_date=_to_date(r.get("value_date") or r.get("date")),
                    reference=r.get("utr") or r.get("ref") or r.get("cheque_no"),
                    narration=r.get("narration") or r.get("description"),
                    raw=r,
                )
            )
        elif source == "ledger":
            out.append(
                Entry(
                    id=r.get("id") or f"ldgr_{i}",
                    source=source,
                    amount_paise=_to_paise(r.get("amount", 0)),
                    value_date=_to_date(r.get("date")),
                    reference=r.get("external_ref") or r.get("ref"),
                    narration=r.get("memo") or r.get("description"),
                    raw=r,
                )
            )
        else:  # pragma: no cover
            raise ValueError(f"unknown source {source}")
    return out
