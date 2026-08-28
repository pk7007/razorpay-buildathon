"""Deterministic + structural reconciliation.

A union-find over entry ids. Each rule contributes merge edges with a rule name,
a confidence and a rationale; connected components become match groups. Rules run
strongest-first and never guess: any ambiguity (more than one viable candidate)
is left for the residual resolver instead of being forced.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from itertools import combinations

from .audit import AuditLog
from .config import SETTINGS
from .models import Entry, MatchGroup, Stage

_STAGE_RANK: dict[Stage, int] = {"deterministic": 0, "structural": 1, "heuristic": 2, "agent": 2}


class _UF:
    def __init__(self, ids: list[str]) -> None:
        self.parent = {i: i for i in ids}
        self.edges: list[tuple[str, str, str, Stage, float, str]] = []

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str, rule: str, stage: Stage, conf: float, why: str) -> None:
        self.edges.append((a, b, rule, stage, conf, why))
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def components(self) -> dict[str, list[str]]:
        comp: dict[str, list[str]] = defaultdict(list)
        for i in self.parent:
            comp[self.find(i)].append(i)
        return comp


def reconcile(
    entries: list[Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[Entry]]:
    """Return (match_groups, residual_entries) — residual = entries in no multi-entry group."""
    by_id = {e.id: e for e in entries}
    uf = _UF([e.id for e in entries])
    tol = SETTINGS.amount_tolerance_paise
    lag = SETTINGS.settlement_lag_days

    _rule_exact_reference(entries, uf, tol)
    _rule_payment_to_settlement(entries, uf, by_id, tol, lag)
    _rule_merged_bank_credit(entries, uf, by_id, tol)

    # ---- assemble groups from connected components --------------------------
    groups: list[MatchGroup] = []
    residual: list[Entry] = []
    edges_by_pair = _edges_index(uf)
    n = 0
    for members in uf.components().values():
        if len(members) < 2:
            residual.append(by_id[members[0]])
            continue
        n += 1
        stage, conf, rules, why = _summarize(members, edges_by_pair)
        gross = max(by_id[m].amount_paise for m in members)
        g = MatchGroup(
            group_id=f"G{n:04d}",
            entry_ids=sorted(members),
            stage=stage,
            rule=", ".join(sorted(rules)),
            confidence=round(conf, 4),
            rationale=why,
            amount_paise=gross,
        )
        groups.append(g)
        audit.record(
            stage=stage,
            rule=g.rule,
            inputs=g.entry_ids,
            outcome="matched",
            confidence=g.confidence,
            rationale=why,
        )

    groups.sort(key=lambda g: g.group_id)
    return groups, residual


# --------------------------------------------------------------------------- rules


def _rule_exact_reference(entries: list[Entry], uf: _UF, tol: int) -> None:
    """Entries that share a cleaned reference (order_id / UTR) and agree on amount."""
    by_ref: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.reference:
            by_ref[e.reference.strip().lower()].append(e)

    for ref, bucket in by_ref.items():
        if len(bucket) < 2:
            continue
        # cluster the bucket by amount so a shared ref with incompatible amounts
        # (e.g. bank charge deducted) is NOT force-joined
        bucket.sort(key=lambda e: e.amount_paise)
        cluster: list[Entry] = [bucket[0]]
        for e in bucket[1:]:
            if abs(e.amount_paise - cluster[-1].amount_paise) <= tol:
                cluster.append(e)
            else:
                _join_all(uf, cluster, "exact-reference@v2", "deterministic", 1.0,
                          f"shared reference {ref!r}, amounts equal within ₹{tol/100:.2f}")
                cluster = [e]
        _join_all(uf, cluster, "exact-reference@v2", "deterministic", 1.0,
                  f"shared reference {ref!r}, amounts equal within ₹{tol/100:.2f}")


class _Unit:
    """A current connected component, viewed as one atomic reconciliation unit."""

    __slots__ = ("root", "ids", "gross", "net", "dates", "consumed")

    def __init__(self, root: str, members: list[Entry]) -> None:
        self.root = root
        self.ids = [e.id for e in members]
        # payment and ledger both carry the GROSS sale amount -> take one side, not the sum
        pay = sum(e.amount_paise for e in members if e.source == "payment")
        ldg = sum(e.amount_paise for e in members if e.source == "ledger")
        setl = sum(e.amount_paise for e in members if e.source == "settlement")
        bank = sum(e.amount_paise for e in members if e.source == "bank")
        self.gross = pay or ldg or max((e.amount_paise for e in members), default=0)
        self.net = setl or bank or max((e.amount_paise for e in members), default=0)
        self.dates = {e.value_date for e in members}
        self.consumed = False


def _units_with(uf: _UF, by_id: dict[str, Entry], source: str) -> list[_Unit]:
    comps: dict[str, list[Entry]] = defaultdict(list)
    for eid, e in by_id.items():
        comps[uf.find(eid)].append(e)
    return [
        _Unit(root, members)
        for root, members in comps.items()
        if any(e.source == source for e in members)
    ]


def _within(unit_dates: set[date], target: date, window: int) -> bool:
    return any(abs((d - target).days) <= window for d in unit_dates)


def _rule_payment_to_settlement(
    entries: list[Entry], uf: _UF, by_id: dict[str, Entry], tol: int, lag: int
) -> None:
    """Link each settlement to the payment component(s) it pays out.

    Identity (holds to the paise):  sum(payment gross)  ==  net + fee + GST.
    1:1 links resolve first (unambiguous), clearing noise for split batches, which
    then need a *unique same-day* subset that hits the identity exactly."""
    settlements = [e for e in entries if e.source == "settlement"]
    units = [u for u in _units_with(uf, by_id, "payment")
             if not any(by_id[i].source == "settlement" for i in u.ids)]

    def target(s: Entry) -> int:
        return s.amount_paise + s.fee_paise + s.tax_paise

    def window_units(s: Entry) -> list[_Unit]:
        expect = s.value_date - timedelta(days=lag)
        return [u for u in units if not u.consumed and _within(u.dates, expect, 1)]

    pending = list(settlements)
    changed = True
    while changed:
        changed = False
        for s in list(pending):
            cands = [u for u in window_units(s) if u.gross == target(s)]
            if len(cands) == 1:
                _link_unit(uf, cands[0], s, "payment-to-settlement@v1", 0.98,
                           _p2s_why(s, cands[0], 1))
                cands[0].consumed = True
                pending.remove(s)
                changed = True

    for s in list(pending):
        subset = _subset_units(window_units(s), target(s), exact=True, max_k=6,
                               key=lambda u: u.gross, common_date=True)
        if subset:
            for u in subset:
                _link_unit(uf, u, s, "split-settlement@v1", 0.92, _p2s_why(s, u, len(subset)))
                u.consumed = True
            pending.remove(s)


def _rule_merged_bank_credit(
    entries: list[Entry], uf: _UF, by_id: dict[str, Entry], tol: int
) -> None:
    """Link each positive bank credit to the settlement component(s) that produced it.

    Identity:  bank credit  ==  sum(settlement net)  on the same value date."""
    banks = [e for e in entries if e.source == "bank" and e.amount_paise > 0]
    units = _units_with(uf, by_id, "settlement")

    def window_units(b: Entry) -> list[_Unit]:
        return [u for u in units
                if not u.consumed
                and uf.find(u.root) != uf.find(b.id)
                and _within(u.dates, b.value_date, 1)]

    pending = list(banks)
    changed = True
    while changed:
        changed = False
        for b in list(pending):
            cands = [u for u in window_units(b) if u.net == b.amount_paise]
            if len(cands) == 1:
                _link_unit(uf, cands[0], b, "bank-to-settlement@v1", 0.96,
                           f"bank credit ₹{b.amount_paise/100:,.2f} on {b.value_date} "
                           f"equals settlement net {cands[0].ids}")
                cands[0].consumed = True
                pending.remove(b)
                changed = True

    for b in list(pending):
        subset = _subset_units(window_units(b), b.amount_paise, exact=True, max_k=4,
                               key=lambda u: u.net, common_date=True)
        if subset and len(subset) > 1:
            for u in subset:
                _link_unit(uf, u, b, "merged-bank-credit@v1", 0.9,
                           f"bank credit ₹{b.amount_paise/100:,.2f} on {b.value_date} "
                           f"= {len(subset)} settlement payouts combined")
                u.consumed = True




def _link_unit(uf: _UF, u: _Unit, e: Entry, rule: str, conf: float, why: str) -> None:
    uf.union(u.ids[0], e.id, rule, "structural", conf, why)


def _p2s_why(s: Entry, u: _Unit, k: int) -> str:
    lag = (s.value_date - min(u.dates)).days
    return (
        f"{k} payment unit(s) sum to ₹{(s.amount_paise+s.fee_paise+s.tax_paise)/100:,.2f} gross "
        f"= settlement net ₹{s.amount_paise/100:,.2f} + fee ₹{s.fee_paise/100:,.2f} "
        f"+ GST ₹{s.tax_paise/100:,.2f}, T+{lag}"
    )


_MAX_COMBOS = 400_000


def _subset_units(
    pool: list[_Unit], target: int, *, exact: bool, max_k: int, key, common_date: bool
) -> list[_Unit] | None:
    """Unique subset (size 2..max_k) of unconsumed units whose ``key`` sums to ``target``.

    ``exact``       -> require an exact paise match (an accounting identity).
    ``common_date`` -> the subset's units must share at least one calendar date
                       (a real split batch / merged payout happens on one day).
    Returns None on no match OR ambiguity (>1 subset) — ambiguity is never guessed.

    k=2 and k=3 use a two-pointer scan (O(n) / O(n^2)); k>=4 falls back to bounded
    enumeration."""
    tol = 0 if exact else 1
    pool = sorted((u for u in pool if not u.consumed and 0 < key(u) <= target + tol), key=key)
    if len(pool) > 60:
        return None

    if common_date:
        # a real split batch / merged payout lands on ONE day: search each day
        # independently, then require a single distinct winner across all days
        by_day: dict = defaultdict(list)
        for u in pool:
            for d in u.dates:
                by_day[d].append(u)
        seen: set[frozenset[int]] = set()
        winners: list[list[_Unit]] = []
        for units in by_day.values():
            w = _subset_units(units, target, exact=exact, max_k=max_k,
                              key=key, common_date=False)
            if w is not None:
                sig = frozenset(id(u) for u in w)
                if sig not in seen:
                    seen.add(sig)
                    winners.append(w)
        return winners[0] if len(winners) == 1 else None

    hits: list[tuple[_Unit, ...]] = []

    def add(combo) -> bool:  # returns True once ambiguous (>1 distinct subset)
        hits.append(combo)
        return len(hits) > 1

    v = [key(u) for u in pool]
    vmap = {id(u): val for u, val in zip(pool, v)}
    n = len(pool)

    # k = 2 : two pointers
    i, j = 0, n - 1
    while i < j:
        s = v[i] + v[j]
        if abs(s - target) <= tol:
            if add((pool[i], pool[j])):
                return None
            i += 1
            j -= 1
        elif s < target:
            i += 1
        else:
            j -= 1
    if len(hits) == 1:
        return list(hits[0])

    # k = 3 : fix one, two pointers on the rest
    if max_k >= 3:
        for a in range(n - 2):
            lo, hi = a + 1, n - 1
            while lo < hi:
                s = v[a] + v[lo] + v[hi]
                if abs(s - target) <= tol:
                    if add((pool[a], pool[lo], pool[hi])):
                        return None
                    lo += 1
                    hi -= 1
                elif s < target:
                    lo += 1
                else:
                    hi -= 1
        if len(hits) == 1:
            return list(hits[0])

    # k >= 4 : bounded enumeration (rare — large split batches)
    budget = _MAX_COMBOS
    for k in range(4, min(max_k, n) + 1):
        for combo in combinations(pool, k):
            budget -= 1
            if budget <= 0:
                return None
            if abs(sum(vmap[id(u)] for u in combo) - target) <= tol and add(combo):
                return None
        if len(hits) == 1:
            return list(hits[0])
    return list(hits[0]) if len(hits) == 1 else None


# --------------------------------------------------------------------------- helpers


def _join_all(
    uf: _UF, members: list[Entry], rule: str, stage: Stage, conf: float, why: str
) -> None:
    for a, b in zip(members, members[1:]):
        uf.union(a.id, b.id, rule, stage, conf, why)


def _edges_index(uf: _UF) -> dict[frozenset[str], list[tuple[str, Stage, float, str]]]:
    idx: dict[frozenset[str], list[tuple[str, Stage, float, str]]] = defaultdict(list)
    for a, b, rule, stage, conf, why in uf.edges:
        idx[frozenset((a, b))].append((rule, stage, conf, why))
    return idx


def _summarize(
    members: list[str], edges_by_pair: dict[frozenset[str], list[tuple[str, Stage, float, str]]]
) -> tuple[Stage, float, set[str], str]:
    member_set = set(members)
    relevant = [
        rec
        for pair, recs in edges_by_pair.items()
        if pair <= member_set
        for rec in recs
    ]
    if not relevant:  # pragma: no cover
        return "structural", 0.5, {"unknown"}, "grouped"
    stage = max((r[1] for r in relevant), key=lambda s: _STAGE_RANK[s])
    conf = min(r[2] for r in relevant)
    rules = {r[0] for r in relevant}
    why = max(relevant, key=lambda r: len(r[3]))[3]
    return stage, conf, rules, why
