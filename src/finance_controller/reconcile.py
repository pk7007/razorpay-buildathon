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
from .models import DEDUCTION_SOURCES, Entry, MatchGroup, Stage
from .money import DEFAULT_CURRENCY, fmt

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
) -> tuple[list[MatchGroup], list[Entry], set[str]]:
    """Return (match_groups, residual_entries, ambiguous_entry_ids).

    ``ambiguous_entry_ids`` are entries the engine deliberately refused to place
    because several equally valid assignments existed — surfaced so the UI can
    say so instead of quietly reporting them as unpaid.
    """
    by_id = {e.id: e for e in entries}
    uf = _UF([e.id for e in entries])
    tol = SETTINGS.amount_tolerance_paise
    lag = SETTINGS.settlement_lag_days
    unresolved: set[str] = set()

    _rule_exact_reference(entries, uf, tol)
    _rule_link_deductions(entries, uf, by_id, audit)
    _rule_payment_to_settlement(entries, uf, by_id, tol, lag, audit, unresolved)
    _rule_merged_bank_credit(entries, uf, by_id, tol)

    # ---- assemble groups from connected components --------------------------
    groups: list[MatchGroup] = []
    residual: list[Entry] = []
    edges_by_root = _edges_by_root(uf)
    n = 0
    for root, members in uf.components().items():
        if len(members) < 2:
            residual.append(by_id[members[0]])
            continue
        n += 1
        stage, conf, rules, why = _summarize(edges_by_root.get(root, []))
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
    return groups, residual, unresolved


# --------------------------------------------------------------------------- rules


def _references_contradict(unit_refs: set[str], candidate: Entry) -> bool:
    """True when both sides name a reference and none of them agree.

    A missing reference is an absence of evidence and proves nothing. Two
    *different* references are evidence of the opposite: UTR8811 and UTR8812 are
    two payouts, and matching them because the amounts happen to agree is how a
    reconciliation quietly books one payout twice.
    """
    if not unit_refs or not candidate.reference:
        return False
    return candidate.reference.strip().lower() not in unit_refs




def _rule_exact_reference(entries: list[Entry], uf: _UF, tol: int) -> None:
    """Entries that share a cleaned reference (order_id / UTR) and agree on amount."""
    # Bucket by (reference, currency). A reference shared across two currencies
    # says the rows are related, not that 1,000 USD equals 1,000 INR -- and
    # joining them would sum two different units of account into one figure.
    by_ref: dict[tuple[str, str], list[Entry]] = defaultdict(list)
    for e in entries:
        if e.reference:
            by_ref[(e.reference.strip().lower(), e.currency)].append(e)

    for (ref, currency), bucket in by_ref.items():
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
                          f"shared reference {ref!r} in {currency}, amounts equal "
                          f"within {fmt(tol, currency)}")
                cluster = [e]
        _join_all(uf, cluster, "exact-reference@v2", "deterministic", 1.0,
                  f"shared reference {ref!r} in {currency}, amounts equal "
                  f"within {fmt(tol, currency)}")


def _rule_link_deductions(
    entries: list[Entry], uf: _UF, by_id: dict[str, Entry], audit: AuditLog
) -> None:
    """Attach every refund and chargeback to the payment it reduces.

    This must run before the settlement rules. A payment of 1000 refunded by 300
    settles as 700; if the refund is not attached first, the settlement identity
    looks for 1000, fails, and a perfectly ordinary refunded sale is reported as
    an exception. Linking is by explicit reference only -- a refund names its
    payment. Refunds that name nothing become orphan exceptions rather than being
    guessed onto the nearest payment of a similar size.
    """
    # A refund names its payment by whichever handle the exporter used: Razorpay's
    # API says payment_id ("pay_NW3X..."), a CSV export often says order_id
    # ("ORD-88001"). Index BOTH so the link is found either way -- looking only at
    # one made every Razorpay-shaped refund an orphan.
    payments_by_ref: dict[str, Entry] = {}
    for e in entries:
        if e.source != "payment":
            continue
        for handle in (e.reference, e.id):
            if handle:
                payments_by_ref.setdefault(str(handle).strip().lower(), e)

    # refunds against the same payment must be summed, not compared individually:
    # 1000 refunded by 300 then 200 leaves 500 owed, and over-refunding is an error
    claimed: dict[str, int] = defaultdict(int)

    for d in entries:
        if d.source not in DEDUCTION_SOURCES:
            continue
        ref = (d.related_reference or "").strip().lower()
        if not ref:
            continue
        payment = payments_by_ref.get(ref)
        if payment is None:
            continue
        if payment.currency != d.currency:
            audit.record(
                stage="deterministic", rule="deduction-currency-mismatch@v1",
                inputs=[d.id, payment.id], outcome="rejected", confidence=0.0,
                rationale=(
                    f"{d.source} {d.id} is in {d.currency} but payment {payment.id} "
                    f"is in {payment.currency}; refusing to net across currencies"
                ),
            )
            continue
        claimed[payment.id] += d.amount_paise
        kind = "refund" if d.source == "refund" else "chargeback"
        uf.union(
            d.id, payment.id, f"{kind}-to-payment@v1", "deterministic", 1.0,
            f"{kind} {fmt(d.amount_paise, d.currency)} against payment {payment.id} "
            f"({fmt(payment.amount_paise, payment.currency)}), leaving "
            f"{fmt(payment.amount_paise - claimed[payment.id], payment.currency)} owed",
        )

    for pid, total in claimed.items():
        payment = by_id[pid]
        if total > payment.amount_paise:
            audit.record(
                stage="deterministic", rule="over-refund@v1", inputs=[pid],
                outcome="exception", confidence=0.95,
                rationale=(
                    f"deductions of {fmt(total, payment.currency)} exceed payment "
                    f"{pid} of {fmt(payment.amount_paise, payment.currency)} by "
                    f"{fmt(total - payment.amount_paise, payment.currency)}"
                ),
            )


class _Unit:
    """A current connected component, viewed as one atomic reconciliation unit."""

    __slots__ = ("root", "ids", "gross", "net", "dates", "by_source", "consumed",
                 "currency", "deductions", "base_gross", "deduction_events",
                 "refs")

    def __init__(self, root: str, members: list[Entry]) -> None:
        self.root = root
        self.ids = [e.id for e in members]
        self.currency = members[0].currency if members else DEFAULT_CURRENCY
        # payment and ledger both carry the GROSS sale amount -> take one side, not the sum
        pay = sum(e.amount_paise for e in members if e.source == "payment")
        ldg = sum(e.amount_paise for e in members if e.source == "ledger")
        setl = sum(e.amount_paise for e in members if e.source == "settlement")
        bank = sum(e.amount_paise for e in members if e.source == "bank")
        # Refunds and chargebacks reduce what the merchant is owed -- but only
        # the ones that had already happened when the payout was cut. A refund
        # raised three weeks AFTER the payout cannot retroactively shrink it, and
        # a chargeback still under review has not been clawed back at all.
        self.deduction_events = [
            (e.value_date, e.amount_paise)
            for e in members
            if e.source in DEDUCTION_SOURCES and _deduction_is_settled(e)
        ]
        self.deductions = sum(a for _, a in self.deduction_events)
        self.base_gross = pay or ldg or max((e.amount_paise for e in members), default=0)
        self.gross = self.base_gross - self.deductions
        self.net = setl or bank or max((e.amount_paise for e in members), default=0)
        self.dates = {e.value_date for e in members}
        # Every reference the unit carries, so an amount-based rule can notice
        # that a candidate's reference contradicts one already inside the unit.
        self.refs = {e.reference.strip().lower() for e in members if e.reference}
        self.by_source: dict[str, set[date]] = defaultdict(set)
        for e in members:
            self.by_source[e.source].add(e.value_date)
        self.consumed = False

    def gross_as_of(self, when: date) -> int:
        """Gross owed at ``when``: base less only the deductions that had landed."""
        return self.base_gross - sum(a for d, a in self.deduction_events if d <= when)

    def has(self, source: str) -> bool:
        return source in self.by_source

    def dates_of(self, source: str) -> set[date]:
        """Dates of just one leg — a unit must be date-matched on the leg being
        linked, not on any date it happens to contain (a payment date is 2 days
        before its own settlement, so matching on it lets a unit slip into the
        wrong payout window)."""
        return self.by_source.get(source, self.dates)


def _deduction_is_settled(e: Entry) -> bool:
    """Does this deduction actually reduce a payout?

    A refund does. A chargeback only does once it is lost or accepted -- while it
    is open or under review the money is still with the merchant, and treating it
    as deducted would report every live dispute as a shortfall.
    """
    if e.source == "refund":
        return True
    return e.dispute_status in (None, "lost", "accepted")


def _component_sources(uf: _UF, by_id: dict[str, Entry]) -> dict[str, set[str]]:
    """root -> the set of sources present in that component, built in one pass.

    Answering "does this settlement already have its payment leg?" by scanning
    every id per settlement is O(n^2) and dominated the profile at 5k records;
    this makes it O(n) once and a dict lookup thereafter.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for eid, e in by_id.items():
        out[uf.find(eid)].add(e.source)
    return out


def _index_units_by_date(units: list[_Unit], source: str) -> dict[date, list[_Unit]]:
    idx: dict[date, list[_Unit]] = defaultdict(list)
    for u in units:
        for d in u.dates_of(source):
            idx[d].append(u)
    return idx


def _near_dates(idx: dict[date, list[_Unit]], target: date, window: int) -> list[_Unit]:
    """Unconsumed units whose indexed leg falls within `window` days of target."""
    seen: dict[int, _Unit] = {}
    for delta in range(-window, window + 1):
        for u in idx.get(target + timedelta(days=delta), ()):
            if not u.consumed:
                seen[id(u)] = u
    return list(seen.values())


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
    entries: list[Entry], uf: _UF, by_id: dict[str, Entry], tol: int, lag: int,
    audit: AuditLog, unresolved: set[str],
) -> None:
    """Link each settlement to the payment component(s) it pays out.

    Identity (holds to the paise):  sum(payment gross)  ==  net + fee + GST.
    1:1 links resolve first (unambiguous), clearing noise for split batches, which
    then need a *unique same-day* subset that hits the identity exactly."""
    comp_src = _component_sources(uf, by_id)
    # only settlements that do not already have their payment leg
    settlements = [
        e for e in entries
        if e.source == "settlement" and "payment" not in comp_src[uf.find(e.id)]
    ]
    units = [u for u in _units_with(uf, by_id, "payment") if not u.has("settlement")]
    # date-match on the PAYMENT leg specifically (see _Unit.dates_of)
    idx = _index_units_by_date(units, "payment")

    def target(s: Entry) -> int:
        """Gross this payout represents, from its OWN reported components.

        No global fee rate is assumed: whatever the settlement reports as fee,
        tax and TDS is added back to its net. A merchant on a negotiated rate,
        a zero-MDR UPI payout and a TDS-withheld payout all reconcile with the
        same code path.
        """
        return s.amount_paise + s.fee_paise + s.tax_paise + (s.tds_paise or 0)

    def window_units(s: Entry) -> list[_Unit]:
        # never match across currencies: 1000 USD is not 1000 INR
        return [u for u in _near_dates(idx, s.value_date - timedelta(days=lag), 1)
                if u.currency == s.currency]

    # `done` holds ids, not Entry objects: list.remove() on a pydantic model is an
    # O(n) scan of __eq__ calls and dominated the profile at 20k records
    pending = list(settlements)
    done: set[str] = set()
    changed = True
    while changed:
        changed = False
        for s in pending:
            if s.id in done:
                continue
            pool = window_units(s)
            cands = [u for u in pool if u.gross == target(s)] or [
                u for u in pool if abs(u.gross - target(s)) <= ROUNDING_SLACK_PAISE
            ]
            if len(cands) == 1:
                _link_unit(uf, cands[0], s, "payment-to-settlement@v1", 0.98,
                           _p2s_why(s, cands[0], 1))
                cands[0].consumed = True
                done.add(s.id)
                changed = True
    pending = [s for s in pending if s.id not in done]

    # pass 2: split batches, resolved as a constraint problem rather than one
    # settlement at a time (see _assign_subsets)
    for s, subset in _assign_subsets(
        pending, window_units, target,
        key=lambda u, anchor: u.gross_as_of(anchor.value_date), max_k=6,
        audit=audit, unresolved=unresolved,
    ):
        for u in subset:
            _link_unit(uf, u, s, "split-settlement@v1", 0.92, _p2s_why(s, u, len(subset)))
            u.consumed = True
        if s in pending:
            pending.remove(s)

    # pass 3: late payouts. Holds, bank holidays, weekly settlement cycles and
    # cross-period payouts all break the T+lag window -- a July sale paid out in
    # August is the single most common real-world carry-forward. The window
    # widens but the bar rises: the amount must match exactly and be unique, so
    # precision is not traded away for reach.
    for s in list(pending):
        cands = [
            u for u in units
            if not u.consumed
            and u.currency == s.currency
            and u.gross_as_of(s.value_date) == target(s)
            and all(d <= s.value_date for d in u.dates_of("payment"))
            and min((s.value_date - d).days for d in u.dates_of("payment"))
            <= SETTINGS.max_payout_lag_days
        ]
        if len(cands) == 1:
            u = cands[0]
            lag_days = min((s.value_date - d).days for d in u.dates_of("payment"))
            _link_unit(
                uf, u, s, "late-payout@v1", 0.9,
                f"payout {fmt(s.amount_paise, s.currency)} on {s.value_date} settles "
                f"gross {fmt(target(s), s.currency)} from T+{lag_days} "
                f"(beyond the usual T+{lag}); exact and unique amount match",
            )
            u.consumed = True
            pending.remove(s)


def _rule_merged_bank_credit(
    entries: list[Entry], uf: _UF, by_id: dict[str, Entry], tol: int
) -> None:
    """Link each positive bank credit to the settlement component(s) that produced it.

    Identity:  bank credit  ==  sum(settlement net)  on the same value date.

    Two invariants keep this from over-merging: a bank credit that already has its
    settlement leg is done (one payout, one credit), and a settlement unit that
    already has a bank leg has been paid out and cannot fund a second credit."""
    comp_src = _component_sources(uf, by_id)
    banks = [
        e for e in entries
        if e.source == "bank"
        and e.amount_paise > 0
        and "settlement" not in comp_src[uf.find(e.id)]
    ]
    units = [u for u in _units_with(uf, by_id, "settlement") if not u.has("bank")]
    # date-match on the SETTLEMENT leg, not on the unit's payment dates
    idx = _index_units_by_date(units, "settlement")

    def window_units(b: Entry) -> list[_Unit]:
        root = uf.find(b.id)
        return [u for u in _near_dates(idx, b.value_date, 1)
                if uf.find(u.root) != root and u.currency == b.currency]

    pending = list(banks)
    done: set[str] = set()
    changed = True
    while changed:
        changed = False
        for b in pending:
            if b.id in done:
                continue
            cands = [u for u in window_units(b)
                     if u.net == b.amount_paise
                     and not _references_contradict(u.refs, b)]
            if len(cands) == 1:
                _link_unit(uf, cands[0], b, "bank-to-settlement@v1", 0.96,
                           f"bank credit ₹{b.amount_paise/100:,.2f} on {b.value_date} "
                           f"equals settlement net {cands[0].ids}")
                cands[0].consumed = True
                done.add(b.id)
                changed = True
    pending = [b for b in pending if b.id not in done]

    for b, subset in _assign_subsets(
        pending, window_units, lambda x: x.amount_paise,
        key=lambda u, _anchor: u.net, max_k=4
    ):
        if len(subset) < 2:
            continue
        for u in subset:
            _link_unit(uf, u, b, "merged-bank-credit@v1", 0.9,
                       f"bank credit ₹{b.amount_paise/100:,.2f} on {b.value_date} "
                       f"= {len(subset)} settlement payouts combined")
            u.consumed = True


def _assign_subsets(
    pending: list[Entry], window_units, target, *, key, max_k: int,
    audit: AuditLog | None = None, unresolved: set[str] | None = None,
) -> list[tuple[Entry, list[_Unit]]]:
    """Assign a disjoint subset of units to each pending anchor, by constraint
    propagation rather than one anchor at a time.

    Independent subset-sum is ambiguous surprisingly often: on a busy day two
    different payment triples can sum to the same rupee. Solving the anchors
    *together* removes most of that. Enumerate every candidate subset per anchor,
    then repeatedly commit any anchor with exactly one surviving candidate and
    strike the units it claims from everyone else's candidates. Anchors still
    holding several candidates when propagation stalls are genuinely
    undecidable from amounts alone, and are left for the resolver / exception
    queue — the engine never breaks a tie by guessing.
    """
    # keyed by position in `pending` because Entry is a pydantic model (unhashable)
    cands: dict[int, list[tuple[_Unit, ...]]] = {}
    for i, anchor in enumerate(pending):
        pool = window_units(anchor)
        tgt = target(anchor)

        # A single unit that matches exactly IS the answer, even if two of them
        # do and we cannot tell which. Inventing a 4-way "split batch" to explain
        # a payout whose real 1:1 counterpart is sitting right there is how three
        # unrelated sales get fused into one group. Ambiguity between singles is
        # a refusal, not a licence to go looking for arithmetic coincidences.
        singles = [u for u in pool if key(u, anchor) == tgt]
        if singles:
            if audit is not None and len(singles) > 1:
                involved = sorted({eid for u in singles for eid in u.ids})
                if unresolved is not None:
                    unresolved.update(involved)
                audit.record(
                    stage="structural", rule="ambiguous-single@v1", inputs=involved,
                    outcome="exception", confidence=0.0,
                    rationale=(
                        f"{anchor.id} matches {len(singles)} different payment units "
                        f"exactly; amounts alone cannot say which one it paid out"
                    ),
                )
            continue

        found = _all_subsets(pool, tgt, key=lambda u: key(u, anchor), max_k=max_k)
        if found:
            cands[i] = found

    assigned: list[tuple[Entry, list[_Unit]]] = []
    claimed: set[int] = set()
    progress = True
    while progress:
        progress = False
        for i, options in list(cands.items()):
            options = [o for o in options if not any(id(u) in claimed for u in o)]
            if not options:
                del cands[i]
                continue
            cands[i] = options
            if len(options) == 1:
                winner = list(options[0])
                assigned.append((pending[i], winner))
                claimed.update(id(u) for u in winner)
                del cands[i]
                progress = True

    # whatever still has several live candidates is undecidable from amounts
    # alone. Say so, loudly, rather than picking one.
    for i, options in cands.items():
        anchor = pending[i]
        involved = sorted({eid for combo in options for u in combo for eid in u.ids})
        if unresolved is not None:
            unresolved.update(involved)
        if audit is not None:
            audit.record(
                stage="structural", rule="ambiguous-split@v1", inputs=involved,
                outcome="exception", confidence=0.0,
                rationale=(
                    f"{anchor.id} (₹{target(anchor)/100:,.2f}) has {len(options)} equally "
                    f"valid same-day subsets summing to the same amount; amounts alone "
                    f"cannot decide which payments it paid out — needs the settlement "
                    f"recon report or a human"
                ),
            )
    return assigned


_MAX_CANDIDATES = 8

# Gateway fee and the GST on it are each rounded to the paise independently, so
# `net + fee + tax` can miss `sum(gross)` by up to 2 paise through no fault of
# anyone's. This band is arithmetic rounding slack, NOT matching tolerance —
# SETTINGS.amount_tolerance_paise (a rupee) is the separate, much looser
# allowance for cross-source noise like bank charges.
ROUNDING_SLACK_PAISE = 2


def _all_subsets(
    pool: list[_Unit], target: int, *, key, max_k: int, slack: int = ROUNDING_SLACK_PAISE
) -> list[tuple[_Unit, ...]]:
    """Every same-day subset (size 2..max_k) summing to target within ``slack``.

    Capped at ``_MAX_CANDIDATES``: past that the anchor is hopeless to
    disambiguate and enumerating more only costs time.
    """
    pool = [u for u in pool if not u.consumed and 0 < key(u) <= target + slack]
    if len(pool) > 60:
        return []
    by_day: dict[date, list[_Unit]] = defaultdict(list)
    for u in pool:
        for d in u.dates:
            by_day[d].append(u)

    exact: list[tuple[_Unit, ...]] = []
    near: list[tuple[_Unit, ...]] = []
    seen: set[frozenset[int]] = set()
    budget = _MAX_COMBOS
    for units in by_day.values():
        units = sorted(units, key=key)
        for k in range(2, min(max_k, len(units)) + 1):
            for combo in combinations(units, k):
                budget -= 1
                if budget <= 0:
                    return exact or near
                gap = abs(sum(key(u) for u in combo) - target)
                if gap > slack:
                    continue
                sig = frozenset(id(u) for u in combo)
                if sig in seen:
                    continue
                seen.add(sig)
                (exact if gap == 0 else near).append(combo)
                if len(exact) >= _MAX_CANDIDATES:
                    return exact
    # exact arithmetic wins outright; the rounding band is only consulted when
    # nothing balances to the paise, so slack can never *create* ambiguity
    return exact or near


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


def _edges_by_root(uf: _UF) -> dict[str, list[tuple[str, Stage, float, str]]]:
    """Group every merge edge under its final component root.

    Both endpoints of an edge are by definition in the same component once all
    unions are done, so one pass over the edges is enough. (Testing every edge
    against every group instead is O(groups x edges) and was the dominant cost
    of a 20k-record run.)
    """
    idx: dict[str, list[tuple[str, Stage, float, str]]] = defaultdict(list)
    for a, _b, rule, stage, conf, why in uf.edges:
        idx[uf.find(a)].append((rule, stage, conf, why))
    return idx


def _summarize(
    relevant: list[tuple[str, Stage, float, str]]
) -> tuple[Stage, float, set[str], str]:
    if not relevant:  # pragma: no cover
        return "structural", 0.5, {"unknown"}, "grouped"
    stage = max((r[1] for r in relevant), key=lambda s: _STAGE_RANK[s])
    conf = min(r[2] for r in relevant)
    rules = {r[0] for r in relevant}
    why = max(relevant, key=lambda r: len(r[3]))[3]
    return stage, conf, rules, why
