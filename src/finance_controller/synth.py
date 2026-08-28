"""Synthetic benchmark generator.

Produces Razorpay-shaped batch exports (payments, settlements, bank statement,
ledger) with deliberately injected real-world messiness AND a ground-truth
answer key, so reconciliation accuracy can be measured honestly.

Every dataset ships:
  * the four raw exports (what a finance team would download)
  * ``labels.json``  -> intended reconciliation groups  {group_id: [entry_id, ...]}
  * ``truth.json``   -> expected exceptions              {entry_id: category}

The generator is fully deterministic for a given ``(profile, seed)``.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

FEE_RATE = 0.02          # 2% gateway fee
GST_RATE = 0.18          # 18% GST on the fee
SETTLEMENT_LAG = 2       # Razorpay standard T+2 payout
START = date(2026, 7, 1)


@dataclass
class Profile:
    name: str
    days: int
    happy_per_day: tuple[int, int]      # min, max fully-clean flows per day
    split_batches: int                  # "many payments -> one payout" batches
    merged_payouts: int                 # "one bank credit = many settlements"
    duplicates: int                     # double-booked ledger entries
    missing_in_bank: int                # ledger income, payout never landed
    missing_in_ledger: int              # bank credit never booked
    payout_in_transit: int              # settlement raised, bank credit outside window
    bank_charge_cases: int              # bank credit short by a small extra fee


PROFILES: dict[str, Profile] = {
    #                        days  happy/day  split merge dup miss_bank miss_ldgr transit charge
    "clean":     Profile("clean",     6, (4, 6),   1,    0,   0,     0,        0,      1,     0),
    "realistic": Profile("realistic", 10, (5, 9),  3,    2,   2,     2,        2,      2,     2),
    "messy":     Profile("messy",     14, (4, 8),  5,    4,   5,     5,        4,      3,     4),
}


@dataclass
class _Acc:
    payments: list[dict] = field(default_factory=list)
    settlements: list[dict] = field(default_factory=list)
    bank: list[dict] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    labels: dict[str, list[str]] = field(default_factory=dict)
    truth: dict[str, str] = field(default_factory=dict)
    _n: int = 0

    def nid(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n:04d}"


def _rupees(paise: int) -> str:
    return f"{paise / 100:.2f}"


def _fee(gross_paise: int) -> tuple[int, int]:
    fee = round(gross_paise * FEE_RATE)
    tax = round(fee * GST_RATE)
    return fee, tax


def _clean_flow(acc: _Acc, rnd: random.Random, day: date, lag: int) -> None:
    """payment -> settlement (net of fee+GST) -> bank credit -> ledger. All linked."""
    gross = rnd.randrange(20_000, 5_000_00)
    fee, tax = _fee(gross)
    net = gross - fee - tax
    order = f"ORD{rnd.randrange(10**6, 10**7)}"
    utr = f"UTR{rnd.randrange(10**9, 10**10)}"
    settled = day + timedelta(days=lag)

    pid = acc.nid("pay")
    sid = acc.nid("setl")
    bid = acc.nid("bank")
    lid = acc.nid("ldgr")

    acc.payments.append(dict(id=pid, amount=_rupees(gross), created_at=day.isoformat(),
                             order_id=order, method=rnd.choice(["upi", "card", "netbanking"]),
                             status="captured"))
    acc.settlements.append(dict(id=sid, amount=_rupees(net), fees=_rupees(fee), tax=_rupees(tax),
                                settled_at=settled.isoformat(), utr=utr))
    acc.bank.append(dict(id=bid, amount=_rupees(net), value_date=settled.isoformat(),
                         utr=utr, narration=f"NEFT RAZORPAY {utr}", type="credit"))
    acc.ledger.append(dict(id=lid, amount=_rupees(gross), date=day.isoformat(),
                           external_ref=order, memo=f"Sale {order}"))
    acc.labels[acc.nid("g")] = [pid, sid, bid, lid]


def _split_batch(acc: _Acc, rnd: random.Random, day: date, lag: int) -> None:
    """3-5 payments in a day -> one aggregated settlement + bank credit."""
    k = rnd.randrange(3, 6)
    utr = f"UTR{rnd.randrange(10**9, 10**10)}"
    settled = day + timedelta(days=lag)
    group: list[str] = []
    net_total = 0
    fee_total = 0
    tax_total = 0
    for _ in range(k):
        gross = rnd.randrange(15_000, 2_000_00)
        fee, tax = _fee(gross)
        net_total += gross - fee - tax
        fee_total += fee
        tax_total += tax
        order = f"ORD{rnd.randrange(10**6, 10**7)}"
        pid = acc.nid("pay")
        lid = acc.nid("ldgr")
        acc.payments.append(dict(id=pid, amount=_rupees(gross), created_at=day.isoformat(),
                                 order_id=order, method="upi", status="captured"))
        acc.ledger.append(dict(id=lid, amount=_rupees(gross), date=day.isoformat(),
                               external_ref=order, memo=f"Sale {order}"))
        group += [pid, lid]
    sid = acc.nid("setl")
    bid = acc.nid("bank")
    acc.settlements.append(dict(id=sid, amount=_rupees(net_total), fees=_rupees(fee_total),
                                tax=_rupees(tax_total), settled_at=settled.isoformat(), utr=utr))
    acc.bank.append(dict(id=bid, amount=_rupees(net_total), value_date=settled.isoformat(),
                         utr=utr, narration=f"NEFT RAZORPAY {utr} BATCH", type="credit"))
    group += [sid, bid]
    acc.labels[acc.nid("g")] = group


def _merged_payout(acc: _Acc, rnd: random.Random, day: date, lag: int) -> None:
    """Two settlements on the same day arrive as a single bank credit."""
    settled = day + timedelta(days=lag)
    total = 0
    group: list[str] = []
    for _ in range(2):
        gross = rnd.randrange(30_000, 3_000_00)
        fee, tax = _fee(gross)
        net = gross - fee - tax
        total += net
        order = f"ORD{rnd.randrange(10**6, 10**7)}"
        utr = f"UTR{rnd.randrange(10**9, 10**10)}"
        pid = acc.nid("pay")
        sid = acc.nid("setl")
        lid = acc.nid("ldgr")
        acc.payments.append(dict(id=pid, amount=_rupees(gross), created_at=day.isoformat(),
                                 order_id=order, method="card", status="captured"))
        acc.settlements.append(dict(id=sid, amount=_rupees(net), fees=_rupees(fee),
                                    tax=_rupees(tax), settled_at=settled.isoformat(), utr=utr))
        acc.ledger.append(dict(id=lid, amount=_rupees(gross), date=day.isoformat(),
                               external_ref=order, memo=f"Sale {order}"))
        group += [pid, sid, lid]
    bid = acc.nid("bank")
    acc.bank.append(dict(id=bid, amount=_rupees(total), value_date=settled.isoformat(),
                         utr="", narration="NEFT RAZORPAY CONSOLIDATED PAYOUT", type="credit"))
    group.append(bid)
    acc.labels[acc.nid("g")] = group


def _duplicate(acc: _Acc, rnd: random.Random, day: date, lag: int) -> None:
    """A clean flow, but the ledger entry is booked twice."""
    gross = rnd.randrange(20_000, 1_500_00)
    fee, tax = _fee(gross)
    net = gross - fee - tax
    order = f"ORD{rnd.randrange(10**6, 10**7)}"
    utr = f"UTR{rnd.randrange(10**9, 10**10)}"
    settled = day + timedelta(days=lag)
    pid = acc.nid("pay")
    sid = acc.nid("setl")
    bid = acc.nid("bank")
    lid = acc.nid("ldgr")
    dup = acc.nid("ldgr")
    acc.payments.append(dict(id=pid, amount=_rupees(gross), created_at=day.isoformat(),
                             order_id=order, method="upi", status="captured"))
    acc.settlements.append(dict(id=sid, amount=_rupees(net), fees=_rupees(fee), tax=_rupees(tax),
                                settled_at=settled.isoformat(), utr=utr))
    acc.bank.append(dict(id=bid, amount=_rupees(net), value_date=settled.isoformat(),
                         utr=utr, narration=f"NEFT RAZORPAY {utr}", type="credit"))
    acc.ledger.append(dict(id=lid, amount=_rupees(gross), date=day.isoformat(),
                           external_ref=order, memo=f"Sale {order}"))
    acc.ledger.append(dict(id=dup, amount=_rupees(gross), date=day.isoformat(),
                           external_ref=order, memo=f"Sale {order} (re-entry)"))
    acc.labels[acc.nid("g")] = [pid, sid, bid, lid]
    acc.truth[dup] = "duplicate"


def _missing_in_bank(acc: _Acc, rnd: random.Random, day: date) -> None:
    """Payment captured + booked, but the payout was put on hold -> never in bank."""
    gross = rnd.randrange(25_000, 2_000_00)
    order = f"ORD{rnd.randrange(10**6, 10**7)}"
    pid = acc.nid("pay")
    lid = acc.nid("ldgr")
    acc.payments.append(dict(id=pid, amount=_rupees(gross), created_at=day.isoformat(),
                             order_id=order, method="card", status="captured"))
    acc.ledger.append(dict(id=lid, amount=_rupees(gross), date=day.isoformat(),
                           external_ref=order, memo=f"Sale {order}"))
    # payment + ledger reconcile; the group is INCOMPLETE (no settlement / bank leg)
    acc.labels[acc.nid("g")] = [pid, lid]


def _missing_in_ledger(acc: _Acc, rnd: random.Random, day: date) -> None:
    """A direct customer bank transfer that never got booked."""
    amt = rnd.randrange(10_000, 1_200_00)
    bid = acc.nid("bank")
    acc.bank.append(dict(id=bid, amount=_rupees(amt), value_date=day.isoformat(),
                         utr=f"UTR{rnd.randrange(10**9, 10**10)}",
                         narration="IMPS INWARD CUSTOMER TRANSFER", type="credit"))
    acc.truth[bid] = "missing_in_ledger"


def _payout_in_transit(acc: _Acc, rnd: random.Random, last_day: date) -> None:
    """Recent sale: payment + settlement present, bank credit lands after the file window."""
    pay_day = last_day - timedelta(days=1)
    gross = rnd.randrange(20_000, 1_500_00)
    fee, tax = _fee(gross)
    net = gross - fee - tax
    order = f"ORD{rnd.randrange(10**6, 10**7)}"
    utr = f"UTR{rnd.randrange(10**9, 10**10)}"
    pid = acc.nid("pay")
    sid = acc.nid("setl")
    lid = acc.nid("ldgr")
    acc.payments.append(dict(id=pid, amount=_rupees(gross), created_at=pay_day.isoformat(),
                             order_id=order, method="upi", status="captured"))
    acc.settlements.append(dict(id=sid, amount=_rupees(net), fees=_rupees(fee), tax=_rupees(tax),
                                settled_at=last_day.isoformat(), utr=utr))
    acc.ledger.append(dict(id=lid, amount=_rupees(gross), date=pay_day.isoformat(),
                           external_ref=order, memo=f"Sale {order}"))
    # payment + settlement + ledger reconcile; bank credit is expected AFTER the window
    acc.labels[acc.nid("g")] = [pid, sid, lid]


def _bank_charge(acc: _Acc, rnd: random.Random, day: date, lag: int) -> None:
    """Clean flow, but the bank deducted an extra processing charge from the credit."""
    gross = rnd.randrange(30_000, 2_500_00)
    fee, tax = _fee(gross)
    net = gross - fee - tax
    charge = rnd.randrange(300, 1800)
    order = f"ORD{rnd.randrange(10**6, 10**7)}"
    utr = f"UTR{rnd.randrange(10**9, 10**10)}"
    settled = day + timedelta(days=lag)
    pid = acc.nid("pay")
    sid = acc.nid("setl")
    bid = acc.nid("bank")
    lid = acc.nid("ldgr")
    acc.payments.append(dict(id=pid, amount=_rupees(gross), created_at=day.isoformat(),
                             order_id=order, method="netbanking", status="captured"))
    acc.settlements.append(dict(id=sid, amount=_rupees(net), fees=_rupees(fee), tax=_rupees(tax),
                                settled_at=settled.isoformat(), utr=utr))
    acc.bank.append(dict(id=bid, amount=_rupees(net - charge), value_date=settled.isoformat(),
                         utr=utr, narration=f"NEFT RAZORPAY {utr} (LESS CHGS)", type="credit"))
    acc.ledger.append(dict(id=lid, amount=_rupees(gross), date=day.isoformat(),
                           external_ref=order, memo=f"Sale {order}"))
    # payment<->settlement<->ledger reconcile; the bank line is the exception
    acc.labels[acc.nid("g")] = [pid, sid, lid]
    acc.truth[bid] = "fee_mismatch"


def generate(profile: str = "realistic", seed: int = 7) -> dict:
    p = PROFILES[profile]
    rnd = random.Random(f"{profile}:{seed}")
    acc = _Acc()
    days = [START + timedelta(days=i) for i in range(p.days)]

    for day in days:
        for _ in range(rnd.randrange(*p.happy_per_day)):
            _clean_flow(acc, rnd, day, SETTLEMENT_LAG)

    for _ in range(p.split_batches):
        _split_batch(acc, rnd, rnd.choice(days[:-1]), SETTLEMENT_LAG)
    for _ in range(p.merged_payouts):
        _merged_payout(acc, rnd, rnd.choice(days[:-1]), SETTLEMENT_LAG)
    for _ in range(p.duplicates):
        _duplicate(acc, rnd, rnd.choice(days[:-1]), SETTLEMENT_LAG)
    early = days[: max(1, p.days // 2)]      # old enough that a payout should have landed
    for _ in range(p.missing_in_bank):
        _missing_in_bank(acc, rnd, rnd.choice(early))
    for _ in range(p.missing_in_ledger):
        _missing_in_ledger(acc, rnd, rnd.choice(days))
    for _ in range(p.payout_in_transit):
        _payout_in_transit(acc, rnd, days[-1])
    for _ in range(p.bank_charge_cases):
        _bank_charge(acc, rnd, rnd.choice(days[:-1]), SETTLEMENT_LAG)

    # shuffle each export so ordering carries no signal
    for rows in (acc.payments, acc.settlements, acc.bank, acc.ledger):
        rnd.shuffle(rows)

    return {
        "profile": profile,
        "seed": seed,
        "payments": acc.payments,
        "settlements": acc.settlements,
        "bank": acc.bank,
        "ledger": acc.ledger,
        "labels": acc.labels,
        "truth": acc.truth,
    }


def write_dataset(out_dir: str | Path, profile: str = "realistic", seed: int = 7) -> Path:
    data = generate(profile, seed)
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)

    for source in ("payments", "settlements", "bank", "ledger"):
        _write_csv(base / f"{source}.csv", data[source])
    (base / "labels.json").write_text(json.dumps(data["labels"], indent=2), encoding="utf-8")
    (base / "truth.json").write_text(json.dumps(data["truth"], indent=2), encoding="utf-8")
    return base


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv

    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
