"""Money and currency.

Two rules this module exists to enforce:

1. **Every amount carries a currency.** ``₹1000`` and ``$1000`` are not the same
   number and must never compare equal. Amounts are integer *minor units*
   (paise, cents) — never floats.
2. **Every derived amount carries its provenance.** A fee the source actually
   reported and a fee we inferred from a rate card are different kinds of fact,
   and a reconciliation that silently treats an estimate as a measurement is
   lying. ``Provenance`` makes that distinction impossible to lose.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Provenance = Literal["actual", "estimated", "absent"]

# minor units per major unit. All the currencies here happen to be 100, but the
# table is what stops someone assuming that (JPY and KWD do not play along).
_MINOR_UNITS: dict[str, int] = {
    "INR": 100, "USD": 100, "EUR": 100, "GBP": 100, "AED": 100,
    "SGD": 100, "AUD": 100, "CAD": 100,
    "JPY": 1,          # no minor unit
    "KWD": 1000,       # three
    "BHD": 1000,
}

_SYMBOLS: dict[str, str] = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED ",
    "SGD": "S$", "AUD": "A$", "CAD": "C$", "JPY": "¥",
}

DEFAULT_CURRENCY = "INR"


class CurrencyMismatch(ValueError):
    """Raised when two amounts in different currencies are combined."""


def is_supported(code: str) -> bool:
    return code.upper() in _MINOR_UNITS


def normalize_code(code: str | None) -> str:
    """Best-effort currency code. Unknown/blank falls back to the default."""
    if not code:
        return DEFAULT_CURRENCY
    c = str(code).strip().upper()
    if c in ("RS", "RS.", "INR.", "₹"):
        return "INR"
    if c in ("$", "US$", "USD$"):
        return "USD"
    return c if c in _MINOR_UNITS else DEFAULT_CURRENCY


def minor_units(currency: str) -> int:
    return _MINOR_UNITS.get(currency.upper(), 100)


def fmt(minor: int, currency: str = DEFAULT_CURRENCY, *, decimals: bool = True) -> str:
    """Format minor units for humans. ₹1,23,456.78 style for INR."""
    currency = normalize_code(currency)
    div = minor_units(currency)
    sym = _SYMBOLS.get(currency, currency + " ")
    major = minor / div if div else minor
    places = 2 if (div > 1 and decimals) else 0
    if currency == "INR":
        body = _indian_grouping(abs(major), places)
    else:
        body = f"{abs(major):,.{places}f}"
    sign = "-" if minor < 0 else ""
    return f"{sign}{sym}{body}"


def _indian_grouping(value: float, places: int) -> str:
    """1234567.89 -> '12,34,567.89' (lakh/crore grouping)."""
    s = f"{value:.{places}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    return f"{whole}.{frac}" if frac else whole


@dataclass(frozen=True)
class Amount:
    """An amount, its currency, and where the number came from."""

    minor: int
    currency: str = DEFAULT_CURRENCY
    provenance: Provenance = "actual"

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", normalize_code(self.currency))

    @property
    def is_estimated(self) -> bool:
        return self.provenance == "estimated"

    def _check(self, other: Amount) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(
                f"cannot combine {self.currency} and {other.currency}"
            )

    def __add__(self, other: Amount) -> Amount:
        self._check(other)
        return Amount(self.minor + other.minor, self.currency, _weakest(self, other))

    def __sub__(self, other: Amount) -> Amount:
        self._check(other)
        return Amount(self.minor - other.minor, self.currency, _weakest(self, other))

    def __str__(self) -> str:
        out = fmt(self.minor, self.currency)
        return f"~{out}" if self.is_estimated else out


def _weakest(a: Amount, b: Amount) -> Provenance:
    """Estimation is contagious: any estimated input makes the result estimated."""
    if "estimated" in (a.provenance, b.provenance):
        return "estimated"
    if "absent" in (a.provenance, b.provenance):
        return "absent"
    return "actual"


def same_currency(*codes: str | None) -> bool:
    normalized = {normalize_code(c) for c in codes}
    return len(normalized) <= 1
