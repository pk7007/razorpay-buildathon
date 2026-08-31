"""Fee schedules, tax, TDS, and the settlement equation.

The engine used to assume one identity for every transaction on the planet::

    gross == net + fee + GST          # fee = 2%, GST = 18% of fee

That is false the moment a merchant has a negotiated rate, a different rate per
payment method, a flat-fee instrument like UPI, TDS withheld under 194-O, a
refund inside the payout window, or a chargeback deducted from it.

This module replaces the assumption with a *configurable equation*::

    expected_net = gross
                 - fee - tax_on_fee
                 - tds
                 - refunds
                 - chargebacks
                 + adjustments

Two principles:

* **Actual beats estimated.** If the source reports the fee, that number is used
  and marked ``actual``. Only when it is missing is a rate card applied, and the
  result is marked ``estimated`` -- which then taints everything derived from it.
  An estimate is never silently presented as a measurement.
* **Every term is explainable.** ``SettlementBreakdown.explain()`` renders the
  arithmetic a finance user needs in order to accept or dispute the result.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .money import DEFAULT_CURRENCY, Amount, Provenance, fmt

BPS = 10_000  # basis points in 100%


def _div_round_half_up(numerator: int, denominator: int) -> int:
    """Integer division, halves rounded away from zero. No float involved.

    Two things were wrong with ``round(gross * bps / BPS)``:

    * It goes through a float. This module's whole contract is integer paise,
      and a float divide on a nine-figure paise amount can land a paisa off —
      which is a correctness bug in a tool whose job is to notice a paisa.
    * Python's ``round`` is banker's rounding, so a fee of exactly half a paisa
      rounds to even: 2% of 100.25 gives 2.00, and 2% of 0.25 gives 0.00.
      Payment processors and Indian accounting round halves *up*, so the engine
      would have quietly disagreed with the settlement report on every .5 case.

    Whatever the convention, it has to be a decision rather than a default. This
    is the decision: half away from zero, in integers, everywhere money is
    derived from a rate.
    """
    if denominator == 0:
        return 0
    negative = (numerator < 0) != (denominator < 0)
    n, d = abs(numerator), abs(denominator)
    magnitude = (2 * n + d) // (2 * d)
    return -magnitude if negative else magnitude


@dataclass(frozen=True)
class FeeRule:
    """How a fee is computed for one payment method.

    ``percent_bps`` and ``fixed_minor`` are additive, so (200, 0) is 2%,
    (0, 300) is a flat 3.00, and (150, 200) is 1.5% + 2.00.
    """

    percent_bps: int = 0
    fixed_minor: int = 0
    tax_bps: int = 1800          # tax on the fee itself (GST is 18% in India)
    label: str = ""

    def fee_on(self, gross_minor: int) -> int:
        return _div_round_half_up(gross_minor * self.percent_bps, BPS) + self.fixed_minor

    def tax_on(self, fee_minor: int) -> int:
        return _div_round_half_up(fee_minor * self.tax_bps, BPS)

    def describe(self) -> str:
        bits = []
        if self.percent_bps:
            bits.append(f"{self.percent_bps / 100:g}%")
        if self.fixed_minor:
            bits.append(fmt(self.fixed_minor))
        body = " + ".join(bits) if bits else "no fee"
        return f"{body} (+{self.tax_bps / 100:g}% tax)"


# Indicative Razorpay-shaped defaults. This is a FALLBACK, used only when a
# source does not report its own fee -- never an override of reported data.
# Real merchant rates are negotiated, so this is configuration, not truth.
DEFAULT_SCHEDULE: dict[str, FeeRule] = {
    "upi":        FeeRule(0, 0, 1800, "UPI (zero MDR)"),
    "netbanking": FeeRule(180, 0, 1800, "netbanking"),
    "card":       FeeRule(200, 0, 1800, "domestic card"),
    "card_intl":  FeeRule(300, 0, 1800, "international card"),
    "wallet":     FeeRule(200, 0, 1800, "wallet"),
    "emi":        FeeRule(300, 0, 1800, "EMI"),
    "default":    FeeRule(200, 0, 1800, "default"),
}


@dataclass
class FeeSchedule:
    """A rate card, loadable from JSON so rates are configuration, not code."""

    rules: dict[str, FeeRule] = field(default_factory=lambda: dict(DEFAULT_SCHEDULE))
    tds_bps: int = 0             # e.g. 100 = 1% withheld; 0 = no TDS
    tds_label: str = "TDS"

    def rule_for(self, method: str | None) -> FeeRule:
        key = (method or "").strip().lower()
        return self.rules.get(key) or self.rules.get("default") or FeeRule()

    def tds_on(self, gross_minor: int) -> int:
        return _div_round_half_up(gross_minor * self.tds_bps, BPS) if self.tds_bps else 0

    @classmethod
    def load(cls, path: str | Path | None = None) -> FeeSchedule:
        """Load from JSON; fall back to defaults when absent or unreadable.

        {"tds_bps": 100, "rules": {"upi": {"percent_bps": 0, "fixed_minor": 0}}}
        """
        path = path or os.getenv("FEE_SCHEDULE_PATH")
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        rules = dict(DEFAULT_SCHEDULE)
        for method, spec in (raw.get("rules") or {}).items():
            if isinstance(spec, dict):
                base = rules.get(str(method).lower(), FeeRule())
                rules[str(method).lower()] = FeeRule(
                    percent_bps=int(spec.get("percent_bps", base.percent_bps)),
                    fixed_minor=int(spec.get("fixed_minor", base.fixed_minor)),
                    tax_bps=int(spec.get("tax_bps", base.tax_bps)),
                    label=str(spec.get("label", method)),
                )
        try:
            tds = int(raw.get("tds_bps", 0))
        except (TypeError, ValueError):
            tds = 0
        return cls(rules=rules, tds_bps=tds)


@dataclass
class SettlementBreakdown:
    """Every term of the settlement equation, with provenance and an explanation."""

    currency: str = DEFAULT_CURRENCY
    gross_minor: int = 0
    fee_minor: int = 0
    tax_minor: int = 0
    tds_minor: int = 0
    refund_minor: int = 0
    chargeback_minor: int = 0
    adjustment_minor: int = 0
    fee_provenance: Provenance = "absent"
    rule_label: str = ""

    @property
    def expected_net_minor(self) -> int:
        return (
            self.gross_minor
            - self.fee_minor
            - self.tax_minor
            - self.tds_minor
            - self.refund_minor
            - self.chargeback_minor
            + self.adjustment_minor
        )

    @property
    def is_estimated(self) -> bool:
        return self.fee_provenance == "estimated"

    def expected(self) -> Amount:
        return Amount(
            self.expected_net_minor,
            self.currency,
            "estimated" if self.is_estimated else "actual",
        )

    def difference_from(self, actual_net_minor: int) -> int:
        return actual_net_minor - self.expected_net_minor

    def explain(self, actual_net_minor: int | None = None) -> str:
        """The arithmetic, in the order a finance user reads it."""
        c = self.currency
        est = " (estimated)" if self.is_estimated else ""
        lines = [f"{'gross':<22}{fmt(self.gross_minor, c):>16}"]
        if self.fee_minor:
            tail = f"   [{self.rule_label}]" if self.rule_label else ""
            lines.append(f"{'fee' + est:<22}{fmt(-self.fee_minor, c):>16}{tail}")
        if self.tax_minor:
            lines.append(f"{'tax on fee':<22}{fmt(-self.tax_minor, c):>16}")
        if self.tds_minor:
            lines.append(f"{'TDS':<22}{fmt(-self.tds_minor, c):>16}")
        if self.refund_minor:
            lines.append(f"{'refunds':<22}{fmt(-self.refund_minor, c):>16}")
        if self.chargeback_minor:
            lines.append(f"{'chargebacks':<22}{fmt(-self.chargeback_minor, c):>16}")
        if self.adjustment_minor:
            lines.append(f"{'adjustments':<22}{fmt(self.adjustment_minor, c):>16}")
        lines.append(f"{'expected settlement':<22}{fmt(self.expected_net_minor, c):>16}")
        if actual_net_minor is not None:
            diff = self.difference_from(actual_net_minor)
            lines.append(f"{'actual settlement':<22}{fmt(actual_net_minor, c):>16}")
            verdict = "   MATCHED" if diff == 0 else "   UNEXPLAINED"
            lines.append(f"{'difference':<22}{fmt(diff, c):>16}{verdict}")
        return "\n".join(lines)

    def one_line(self) -> str:
        c = self.currency
        parts = [f"gross {fmt(self.gross_minor, c)}"]
        if self.fee_minor:
            parts.append(f"fee {fmt(self.fee_minor, c)}" + (" (est)" if self.is_estimated else ""))
        if self.tax_minor:
            parts.append(f"tax {fmt(self.tax_minor, c)}")
        if self.tds_minor:
            parts.append(f"TDS {fmt(self.tds_minor, c)}")
        if self.refund_minor:
            parts.append(f"refunds {fmt(self.refund_minor, c)}")
        if self.chargeback_minor:
            parts.append(f"chargebacks {fmt(self.chargeback_minor, c)}")
        return " - ".join(parts) + f" = {fmt(self.expected_net_minor, c)}"


def build_breakdown(
    *,
    gross_minor: int,
    currency: str = DEFAULT_CURRENCY,
    method: str | None = None,
    schedule: FeeSchedule | None = None,
    reported_fee_minor: int | None = None,
    reported_tax_minor: int | None = None,
    reported_tds_minor: int | None = None,
    refund_minor: int = 0,
    chargeback_minor: int = 0,
    adjustment_minor: int = 0,
) -> SettlementBreakdown:
    """Assemble the settlement equation, preferring reported data over the rate card.

    ``reported_*`` values come from the settlement export. When present they are
    authoritative and the result is ``actual``. When absent, the schedule fills
    the gap and the result is ``estimated`` -- a distinction that survives all
    the way to the UI.
    """
    sched = schedule or FeeSchedule()
    rule = sched.rule_for(method)

    if reported_fee_minor is not None:
        fee = reported_fee_minor
        provenance: Provenance = "actual"
        label = "reported by source"
    else:
        fee = rule.fee_on(gross_minor)
        provenance = "estimated"
        label = rule.describe()

    if reported_tax_minor is not None:
        tax = reported_tax_minor
    else:
        tax = rule.tax_on(fee)
        if reported_fee_minor is None:
            provenance = "estimated"

    tds = reported_tds_minor if reported_tds_minor is not None else sched.tds_on(gross_minor)

    return SettlementBreakdown(
        currency=currency,
        gross_minor=gross_minor,
        fee_minor=fee,
        tax_minor=tax,
        tds_minor=tds,
        refund_minor=refund_minor,
        chargeback_minor=chargeback_minor,
        adjustment_minor=adjustment_minor,
        fee_provenance=provenance,
        rule_label=label,
    )
