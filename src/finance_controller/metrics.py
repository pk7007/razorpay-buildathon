"""Pair-based precision / recall against a labelled group set."""
from __future__ import annotations

from itertools import combinations

from .models import MatchGroup


def _pairs(groups: list[list[str]]) -> set[frozenset[str]]:
    out: set[frozenset[str]] = set()
    for g in groups:
        for a, b in combinations(sorted(g), 2):
            out.add(frozenset((a, b)))
    return out


def score(
    predicted: list[MatchGroup],
    labels: dict[str, list[str]] | None,
    total_entries: int,
) -> dict:
    matched_entries = {i for g in predicted for i in g.entry_ids}
    auto_match_rate = len(matched_entries) / total_entries if total_entries else 0.0
    det = sum(1 for g in predicted if g.stage == "deterministic")
    agent = sum(1 for g in predicted if g.stage == "agent")

    out: dict = {
        "total_entries": total_entries,
        "groups": len(predicted),
        "auto_match_rate": round(auto_match_rate, 4),
        "deterministic_share": round(det / len(predicted), 4) if predicted else 0.0,
        "agent_share": round(agent / len(predicted), 4) if predicted else 0.0,
    }

    if labels:
        pred_pairs = _pairs([g.entry_ids for g in predicted])
        true_pairs = _pairs(list(labels.values()))
        tp = len(pred_pairs & true_pairs)
        precision = tp / len(pred_pairs) if pred_pairs else 0.0
        recall = tp / len(true_pairs) if true_pairs else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        out |= {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
    return out
