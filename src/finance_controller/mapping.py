"""Universal column mapping for real bank and accounting exports.

Every bank names its columns differently. HDFC writes "Withdrawal Amt." and
"Deposit Amt."; ICICI writes "Debit"/"Credit"; Tally exports "Particulars"; a
Razorpay CSV writes "created_at". Requiring one exact schema means the upload
path only ever works on data we generated ourselves.

So instead of matching names, this module *scores* them: each internal field has
a list of aliases and patterns, every source column is scored against every
field, and the best assignment wins -- with two safety rules that matter more
than the cleverness:

* **Ambiguity is surfaced, never guessed.** If two columns score equally for
  `amount`, the mapping reports it rather than silently picking one. Mapping the
  wrong column into `amount` would corrupt a reconciliation invisibly, which is
  far worse than refusing to proceed.
* **Nothing is invented.** A field with no plausible column is reported missing
  with the aliases that would have satisfied it, so the fix is obvious.

Split debit/credit columns are recognised and folded into one signed amount,
because that is how most Indian bank statements actually arrive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Source

# field -> (exact aliases, substring patterns). Order matters: exact wins.
_ALIASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "id": (
        ("id", "txn id", "transaction id", "reference no", "ref no", "cheque no",
         "payment id", "entry id", "sl no", "serial"),
        ("txn_id", "transactionid", "transaction_no", "voucher"),
    ),
    "date": (
        ("date", "value date", "transaction date", "txn date", "posting date",
         "created_at", "created at", "settled_at", "settled at", "booking date",
         "value dt", "tran date"),
        ("date", "dt"),
    ),
    "amount": (
        # NOTE: no bare "value" -- it made "Value Date" score as an amount column
        ("amount", "amt", "transaction amount", "txn amount",
         "amount inr", "gross amount", "gross"),
        ("amount", "amt"),
    ),
    "debit": (
        ("debit", "withdrawal", "withdrawal amt", "withdrawal amt.", "withdrawals",
         "dr", "debit amount", "paid out", "outflow"),
        ("withdraw", "debit", "outflow"),
    ),
    "credit": (
        ("credit", "deposit", "deposit amt", "deposit amt.", "deposits", "cr",
         "credit amount", "paid in", "inflow"),
        ("deposit", "credit", "inflow"),
    ),
    "narration": (
        ("narration", "description", "particulars", "remarks", "details",
         "transaction remarks", "memo", "note", "transaction details"),
        ("narrat", "descri", "particular", "remark", "detail", "memo"),
    ),
    "reference": (
        ("utr", "ref", "reference", "rrn", "external_ref", "order_id", "order id",
         "utr no", "utr number", "chq/ref no", "chq no", "instrument no"),
        ("utr", "rrn", "ref", "order"),
    ),
    "currency": (
        ("currency", "ccy", "curr", "currency code"),
        ("currenc", "ccy"),
    ),
    "balance": (
        ("balance", "closing balance", "running balance", "bal"),
        ("balance", "bal"),
    ),
    "fees": (
        ("fee", "fees", "commission", "mdr", "charges", "gateway fee"),
        ("fee", "commission", "mdr", "charge"),
    ),
    "tax": (
        ("tax", "gst", "gst on fee", "service tax", "igst", "cgst"),
        ("gst", "tax"),
    ),
    "tds": (
        ("tds", "tds amount", "withholding", "withholding tax"),
        ("tds", "withhold"),
    ),
    "status": (
        ("status", "state", "txn status", "payment status", "dispute status"),
        ("status", "state"),
    ),
    "method": (
        ("method", "payment method", "mode", "payment mode", "instrument", "channel"),
        ("method", "mode", "instrument", "channel"),
    ),
    "payment_id": (
        ("payment_id", "payment id", "parent payment", "original payment",
         "against payment", "linked payment"),
        ("payment_id", "paymentid", "parent"),
    ),
    "type": (
        ("type", "dr/cr", "drcr", "cr/dr", "transaction type", "txn type"),
        ("dr/cr", "drcr", "type"),
    ),
}

# what each source genuinely cannot reconcile without
_REQUIRED: dict[str, tuple[str, ...]] = {
    "payment": ("amount", "date"),
    "settlement": ("amount", "date"),
    "bank": ("date",),          # amount may arrive as debit/credit instead
    "ledger": ("amount", "date"),
    "refund": ("amount", "date"),
    "chargeback": ("amount", "date"),
}

_NOISE = re.compile(r"[^a-z0-9]+")


def _canon(name: str) -> str:
    return _NOISE.sub(" ", str(name).strip().lower()).strip()


@dataclass
class ColumnMapping:
    """The outcome of mapping one file's headers onto the internal schema."""

    source: Source
    mapping: dict[str, str] = field(default_factory=dict)      # internal -> source column
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    split_amount: bool = False        # amount reconstructed from debit/credit

    @property
    def ok(self) -> bool:
        return not self.missing_required and not self.ambiguous

    def explain(self) -> list[str]:
        out = [f"{internal} <- {src!r}" for internal, src in sorted(self.mapping.items())]
        if self.split_amount:
            out.append("amount <- debit/credit columns combined into a signed value")
        for f, cols in self.ambiguous.items():
            out.append(f"AMBIGUOUS {f}: {cols} — cannot choose safely")
        for f in self.missing_required:
            aliases = ", ".join(_ALIASES[f][0][:4])
            out.append(f"MISSING {f} — expected one of: {aliases}")
        return out


def _score(column: str, field_name: str) -> tuple[int, int]:
    """(strength, preference) for mapping ``column`` onto ``field_name``.

    ``preference`` is the alias's position in the list, so when two columns match
    equally strongly the documented convention wins -- for dates that is
    "value date" over "transaction date", because the value date is when the
    money actually moved, which is what reconciliation cares about.
    """
    exact, patterns = _ALIASES[field_name]
    c = _canon(column)
    for rank, a in enumerate(exact):
        if c == _canon(a):
            return 100, rank
    # word-boundary prefix only: "value date" must not match the alias "value"
    for rank, a in enumerate(exact):
        ca = _canon(a)
        if c.startswith(ca + " ") or ca.startswith(c + " "):
            return 70, rank
    for p in patterns:
        if p in c:
            return 40, len(exact)
    return 0, 99


def detect(source: Source, columns: list[str]) -> ColumnMapping:
    """Map a file's headers onto the internal schema for ``source``."""
    cm = ColumnMapping(source=source)
    cols = [c for c in columns if str(c).strip()]

    scored: dict[str, list[tuple[int, int, str]]] = {}
    for field_name in _ALIASES:
        hits = []
        for c in cols:
            strength, pref = _score(c, field_name)
            if strength > 0:
                hits.append((strength, pref, c))
        if hits:
            scored[field_name] = sorted(hits, key=lambda t: (-t[0], t[1], t[2]))

    taken: set[str] = set()
    # assign strongest evidence first so a column that is a perfect match for one
    # field is not stolen by a field it merely resembles
    for field_name, hits in sorted(scored.items(), key=lambda kv: -kv[1][0][0]):
        available = [h for h in hits if h[2] not in taken]
        if not available:
            continue
        best_strength, best_pref, _ = available[0]
        # a tie only counts as ambiguous when the aliases are equally preferred;
        # "value date" beating "transaction date" is a convention, not a coin toss
        contenders = [
            c for st, pf, c in available if st == best_strength and pf == best_pref
        ]
        if best_strength >= 70 and len(contenders) > 1 and field_name in ("amount", "date"):
            # only genuinely dangerous fields block; a second "remarks" column is
            # not worth stopping a reconciliation over
            cm.ambiguous[field_name] = contenders
            continue
        cm.mapping[field_name] = contenders[0]
        taken.add(contenders[0])

    if "amount" not in cm.mapping and {"debit", "credit"} & set(cm.mapping):
        cm.split_amount = True

    cm.unmapped_columns = [c for c in cols if c not in taken]
    for req in _REQUIRED.get(source, ()):
        if req == "amount" and cm.split_amount:
            continue
        if req not in cm.mapping and req not in cm.ambiguous:
            cm.missing_required.append(req)
    return cm


def apply_mapping(cm: ColumnMapping, rows: list[dict]) -> list[dict]:
    """Rewrite rows into the canonical keys ``normalize()`` expects."""
    out: list[dict] = []
    inv = cm.mapping
    for r in rows:
        rec: dict = {}
        for internal, src_col in inv.items():
            if src_col in r:
                rec[internal] = r[src_col]

        if cm.split_amount:
            debit = _num(rec.pop("debit", None))
            credit = _num(rec.pop("credit", None))
            # a bank statement's two columns become one signed amount: credits in,
            # debits out. Exactly one is normally populated per row.
            if credit:
                rec["amount"] = credit
                rec["type"] = "credit"
            elif debit:
                rec["amount"] = -abs(debit)
                rec["type"] = "debit"
            else:
                rec["amount"] = 0
        else:
            rec.pop("debit", None)
            rec.pop("credit", None)

        # canonical aliases the normalizers read
        if "date" in rec:
            rec.setdefault("value_date", rec["date"])
            rec.setdefault("created_at", rec["date"])
            rec.setdefault("settled_at", rec["date"])
        if "reference" in rec:
            rec.setdefault("utr", rec["reference"])
            rec.setdefault("external_ref", rec["reference"])
            rec.setdefault("order_id", rec["reference"])
        if "narration" in rec:
            rec.setdefault("memo", rec["narration"])
            rec.setdefault("description", rec["narration"])
        if "fees" in rec:
            rec.setdefault("fee", rec["fees"])
        rec["_raw"] = r
        out.append(rec)
    return out


def _num(v) -> float:
    if v in (None, ""):
        return 0.0
    try:
        s = str(v).replace(",", "").replace("₹", "").replace("$", "").strip()
        if not s or s in ("-", "--"):
            return 0.0
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()")
        val = float(s)
        return -val if neg else val
    except (TypeError, ValueError):
        return 0.0
