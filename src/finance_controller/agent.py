"""Stage 2: LLM agent over the residual set only.

Contract:
  * input  = residual entries the deterministic stage could not place
  * output = proposed MatchGroups (stage="agent") + leftover entry ids
  * the agent may NEVER invent or drop an entry; pipeline asserts conservation
"""
from __future__ import annotations

import json

from .audit import AuditLog
from .config import SETTINGS
from .models import Entry, MatchGroup

SYSTEM = """You are a finance reconciliation assistant. You receive a small set of \
unmatched accounting entries from four sources (payment, settlement, bank, ledger). \
Group entries that represent the same real-world money movement (allowing for fees, \
tax, timing gaps, splits and merges). Return STRICT JSON:
{"groups":[{"entry_ids":[...],"confidence":0.0-1.0,"rationale":"..."}]}
Rules: never include an entry id that was not given; if unsure, leave it out; \
a group's amounts must reconcile once fees/tax are accounted for."""


def _prompt(entries: list[Entry]) -> str:
    rows = [
        {
            "id": e.id,
            "source": e.source,
            "amount_paise": e.amount_paise,
            "value_date": e.value_date.isoformat(),
            "reference": e.reference,
            "narration": e.narration,
        }
        for e in entries
    ]
    return json.dumps(rows, indent=2)


def agent_match(
    residual: list[Entry], audit: AuditLog
) -> tuple[list[MatchGroup], list[Entry]]:
    if not residual:
        return [], []

    valid_ids = {e.id for e in residual}
    proposals = _call_llm(_prompt(residual))

    groups: list[MatchGroup] = []
    consumed: set[str] = set()
    for i, p in enumerate(proposals, start=1):
        ids = [x for x in p.get("entry_ids", []) if x in valid_ids and x not in consumed]
        conf = float(p.get("confidence", 0.0))
        rationale = str(p.get("rationale", ""))[:500]
        if len(ids) < 2 or conf < SETTINGS.agent_accept_threshold:
            audit.record(
                stage="agent",
                rule="llm-proposal-rejected@v1",
                inputs=ids,
                outcome="exception",
                confidence=conf,
                rationale=f"below threshold or too small: {rationale}",
            )
            continue
        groups.append(
            MatchGroup(
                group_id=f"a{i:05d}",
                entry_ids=ids,
                stage="agent",
                rule="llm-match@v1",
                confidence=conf,
                rationale=rationale,
            )
        )
        consumed.update(ids)
        audit.record(
            stage="agent",
            rule="llm-match@v1",
            inputs=ids,
            outcome="matched",
            confidence=conf,
            rationale=rationale,
        )

    leftover = [e for e in residual if e.id not in consumed]
    return groups, leftover


def _call_llm(user_prompt: str) -> list[dict]:
    """Return the list of proposed groups. Deterministic: temperature=0."""
    if not SETTINGS.anthropic_api_key:
        # offline / no key -> no proposals, everything becomes an exception
        return []
    from anthropic import Anthropic

    client = Anthropic(api_key=SETTINGS.anthropic_api_key)
    msg = client.messages.create(
        model=SETTINGS.llm_model,
        max_tokens=2000,
        temperature=0,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(block.text for block in msg.content if block.type == "text")
    try:
        return json.loads(text).get("groups", [])
    except json.JSONDecodeError:
        return []
