"""Wire the stages together and write the out/ directory."""
from __future__ import annotations

import json
from pathlib import Path

from .agent import agent_match
from .audit import AuditLog
from .exceptions import build_exceptions
from .ingest import load_from_dir
from .metrics import score
from .models import Entry
from .normalize import normalize
from .reconcile import deterministic_match


def run(input_dir: str | Path, out_dir: str | Path, labels_path: str | Path | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(out / "audit.jsonl")

    raw = load_from_dir(input_dir)
    entries: list[Entry] = []
    for source, rows in raw.items():
        entries.extend(normalize(source, rows))

    det_groups, residual = deterministic_match(entries, audit)
    agent_groups, leftover = agent_match(residual, audit)
    all_groups = det_groups + agent_groups

    exceptions = build_exceptions(leftover)

    # conservation check: every entry is either matched or an exception, exactly once
    matched_ids = [i for g in all_groups for i in g.entry_ids]
    exc_ids = [e.entry_id for e in exceptions]
    assert sorted(matched_ids + exc_ids) == sorted(e.id for e in entries), (
        "conservation violated: entries were invented or dropped"
    )

    (out / "reconciliation.json").write_text(
        json.dumps(
            {
                "groups": [g.model_dump(mode="json") for g in all_groups],
                "exceptions": [e.model_dump(mode="json") for e in exceptions],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _write_exceptions_csv(out / "exceptions.csv", exceptions)

    labels = None
    if labels_path and Path(labels_path).exists():
        labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    metrics = score(all_groups, labels, total_entries=len(entries))
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    return metrics


def _write_exceptions_csv(path: Path, exceptions) -> None:
    lines = ["entry_id,category,confidence,suggested_action"]
    for e in exceptions:
        action = e.suggested_action.replace(",", ";")
        lines.append(f"{e.entry_id},{e.category},{e.confidence},{action}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
