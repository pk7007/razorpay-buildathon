"""Residual resolver — the last, hardest slice of matching.

Two interchangeable backends, same contract:

  input  : the residual entries the deterministic + structural stages could not place
  output : additional MatchGroups (+ the entries still left over)

  * ``llm``       — an LLM proposes groupings with a rationale + confidence (temp 0)
  * ``heuristic`` — a deterministic scorer (amount / date / narration-token overlap)

Both are conservation-checked by the pipeline: the resolver may never invent an
entry id or drop one silently.
"""
from __future__ import annotations

import json
import re

from .audit import AuditLog
from .config import SETTINGS
from .models import Entry, MatchGroup

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def resolve(
    residual: list[Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[Entry], dict]:
    if len(residual) < 2:
        return [], residual, _empty_usage()
    if SETTINGS.has_llm:
        try:
            return _llm_resolve(residual, audit)
        except Exception as exc:  # noqa: BLE001 - fall back, never crash the run
            audit.record(
                stage="agent", rule="llm-error-fallback@v1", inputs=[],
                outcome="rejected", confidence=0.0,
                rationale=f"LLM call failed ({type(exc).__name__}); using heuristic resolver",
            )
    return _heuristic_resolve(residual, audit)


# --------------------------------------------------------------------------- heuristic


def _heuristic_resolve(
    residual: list[Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[Entry], dict]:
    tol = SETTINGS.amount_tolerance_paise
    thr = SETTINGS.resolver_accept_threshold
    used: set[str] = set()
    groups: list[MatchGroup] = []

    scored: list[tuple[float, Entry, Entry, str]] = []
    for i, a in enumerate(residual):
        for b in residual[i + 1 :]:
            if a.source == b.source:
                continue
            s, why = _pair_score(a, b, tol)
            if s >= thr:
                scored.append((s, a, b, why))
    scored.sort(key=lambda t: -t[0])

    n = 0
    for s, a, b, why in scored:
        if a.id in used or b.id in used:
            continue
        used.update((a.id, b.id))
        n += 1
        groups.append(
            MatchGroup(
                group_id=f"H{n:04d}",
                entry_ids=sorted((a.id, b.id)),
                stage="heuristic",
                rule="heuristic-pair@v1",
                confidence=round(s, 4),
                rationale=why,
                amount_paise=max(a.amount_paise, b.amount_paise),
            )
        )
        audit.record(
            stage="heuristic", rule="heuristic-pair@v1", inputs=sorted((a.id, b.id)),
            outcome="matched", confidence=round(s, 4), rationale=why,
        )

    leftover = [e for e in residual if e.id not in used]
    return groups, leftover, _empty_usage()


def _pair_score(a: Entry, b: Entry, tol: int) -> tuple[float, str]:
    amt_gap = abs(a.amount_paise - b.amount_paise)
    if amt_gap <= tol:
        amt = 1.0
    elif amt_gap <= max(tol * 20, abs(a.amount_paise) * 0.03):
        amt = 0.6
    else:
        return 0.0, ""
    day_gap = abs((a.value_date - b.value_date).days)
    date = 1.0 if day_gap <= 3 else 0.6 if day_gap <= 7 else 0.2
    ta, tb = _tokens(a.narration), _tokens(b.narration)
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    ref = 0.0
    if a.reference and b.reference and (
        a.reference.lower() in (b.narration or "").lower()
        or b.reference.lower() in (a.narration or "").lower()
        or a.reference.lower() == b.reference.lower()
    ):
        ref = 1.0
    score = 0.5 * amt + 0.25 * date + 0.15 * jac + 0.1 * ref
    why = (
        f"heuristic: amount gap ₹{amt_gap/100:,.2f}, {day_gap}d apart, "
        f"narration overlap {jac:.0%}" + (", reference echo" if ref else "")
    )
    return score, why


def _tokens(text: str | None) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


# --------------------------------------------------------------------------- llm


_SYSTEM = """You reconcile leftover accounting entries from four sources \
(payment, settlement, bank, ledger). Group entries that represent the SAME real \
money movement, allowing for gateway fees, GST, bank charges, timing gaps and \
splits/merges. Reply with STRICT JSON only:
{"groups":[{"entry_ids":[...],"confidence":0.0-1.0,"rationale":"short reason"}]}
Never output an id that was not given. If unsure, leave the entry out. Every \
group's amounts must plausibly reconcile."""


def _llm_resolve(
    residual: list[Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[Entry], dict]:
    from anthropic import Anthropic

    payload = [
        {
            "id": e.id,
            "source": e.source,
            "amount": round(e.amount_paise / 100, 2),
            "date": e.value_date.isoformat(),
            "reference": e.reference,
            "narration": e.narration,
        }
        for e in residual
    ]
    client = Anthropic(api_key=SETTINGS.anthropic_api_key)
    msg = client.messages.create(
        model=SETTINGS.llm_model,
        max_tokens=2000,
        temperature=0,
        system=_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    usage = {
        "llm_calls": 1,
        "llm_input_tokens": msg.usage.input_tokens,
        "llm_output_tokens": msg.usage.output_tokens,
        "llm_cost_usd": round(
            msg.usage.input_tokens / 1e6 * SETTINGS.llm_input_usd_per_mtok
            + msg.usage.output_tokens / 1e6 * SETTINGS.llm_output_usd_per_mtok,
            4,
        ),
    }

    valid = {e.id for e in residual}
    proposals = _parse_groups(text)
    used: set[str] = set()
    groups: list[MatchGroup] = []
    by_id = {e.id: e for e in residual}
    n = 0
    for p in proposals:
        ids = sorted({x for x in p.get("entry_ids", []) if x in valid and x not in used})
        conf = float(p.get("confidence", 0.0))
        why = str(p.get("rationale", ""))[:400]
        if len(ids) < 2 or conf < SETTINGS.resolver_accept_threshold:
            audit.record(
                stage="agent", rule="llm-proposal-rejected@v1", inputs=ids,
                outcome="rejected", confidence=conf,
                rationale=f"below threshold / too small: {why}",
            )
            continue
        used.update(ids)
        n += 1
        groups.append(
            MatchGroup(
                group_id=f"A{n:04d}", entry_ids=ids, stage="agent",
                rule="llm-match@v1", confidence=round(conf, 4), rationale=why,
                amount_paise=max(by_id[i].amount_paise for i in ids),
            )
        )
        audit.record(
            stage="agent", rule="llm-match@v1", inputs=ids, outcome="matched",
            confidence=round(conf, 4), rationale=why,
        )

    leftover = [e for e in residual if e.id not in used]
    return groups, leftover, usage


def _parse_groups(text: str) -> list[dict]:
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        return json.loads(text).get("groups", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def _empty_usage() -> dict:
    return {"llm_calls": 0, "llm_input_tokens": 0, "llm_output_tokens": 0, "llm_cost_usd": 0.0}
