"""Wire the stages together into a single ``ReconResult``."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from .audit import AuditLog
from .config import SETTINGS
from .exceptions import classify_residual, extract_duplicates
from .ingest import load_dataset, load_from_dir
from .metrics import (
    classify_group,
    money_summary,
    result_fingerprint,
    score_exceptions,
    score_matches,
)
from .models import Entry, ReconResult
from .normalize import normalize
from .reconcile import reconcile
from .resolver import resolve

_SOURCES = ("payment", "settlement", "bank", "ledger", "refund", "chargeback")


def run_rows(
    rows_by_source: dict,
    *,
    dataset: str = "upload",
    labels: dict | None = None,
    truth: dict | None = None,
    check_replay: bool = True,
) -> ReconResult:
    t0 = time.perf_counter()
    audit = AuditLog()

    entries: list[Entry] = []
    for source in _SOURCES:
        entries.extend(normalize(source, rows_by_source.get(source, []) or []))
    entries = _disambiguate_ids(entries)
    by_id = {e.id: e for e in entries}

    groups, residual, ambiguous_ids = reconcile(entries, audit)
    groups, dup_exceptions = extract_duplicates(groups, by_id, audit)

    # anything pulled out as a duplicate is no longer residual, but a group that
    # lost members down to <2 releases its remaining entry back to residual
    residual = [e for e in residual if e.id not in {d.entry_id for d in dup_exceptions}]
    kept_groups = []
    for g in groups:
        if len(g.entry_ids) >= 2:
            kept_groups.append(g)
        elif g.entry_ids:
            residual.append(by_id[g.entry_ids[0]])
    groups = kept_groups

    res_groups, leftover, usage = resolve(residual, audit)
    groups.extend(res_groups)

    exceptions = dup_exceptions + classify_residual(leftover, entries, audit)

    dataset_end = max((e.value_date for e in entries), default=None)
    if dataset_end is not None:
        for g in groups:
            classify_group(
                g, by_id, dataset_end, SETTINGS.settlement_lag_days, ambiguous_ids
            )

    # conservation: every entry is matched exactly once or is an exception exactly once
    matched_ids = [i for g in groups for i in g.entry_ids]
    exc_ids = [e.entry_id for e in exceptions]
    assert sorted(matched_ids + exc_ids) == sorted(e.id for e in entries), (
        "conservation violated — entries were invented or dropped"
    )
    assert len(matched_ids) == len(set(matched_ids)), "an entry is in two groups"

    latency_ms = int((time.perf_counter() - t0) * 1000)
    metrics = score_matches(
        groups, labels, len(entries), latency_ms=latency_ms, usage=usage
    )
    metrics.exception_category_accuracy = score_exceptions(exceptions, truth)
    money = money_summary(entries, groups, exceptions)

    if check_replay:
        fp1 = result_fingerprint(groups, exceptions)
        replay = run_rows(
            rows_by_source, dataset=dataset, labels=labels, truth=truth, check_replay=False
        )
        metrics.replay_stable = fp1 == result_fingerprint(replay.groups, replay.exceptions)

    return ReconResult(
        dataset=dataset,
        generated_at=datetime.now(UTC),
        entries=entries,
        groups=sorted(groups, key=lambda g: g.group_id),
        exceptions=sorted(exceptions, key=lambda e: (-e.amount_paise, e.entry_id)),
        audit=audit.records,
        money=money,
        metrics=metrics,
        resolver_mode="llm" if (SETTINGS.has_llm and usage.get("llm_calls")) else "heuristic",
    )


def _disambiguate_ids(entries: list[Entry]) -> list[Entry]:
    """Make every entry id unique within a run.

    Re-exported statements routinely repeat an id, and two rows sharing one would
    silently collapse into a single entry -- which then trips the conservation
    assertion and fails the whole run. Each repeat gets a ``#n`` suffix so both
    rows survive, stay individually traceable, and can be reported as the
    duplicates they are rather than taking the batch down.
    """
    seen: dict[str, int] = {}
    out: list[Entry] = []
    for e in entries:
        n = seen.get(e.id, 0) + 1
        seen[e.id] = n
        out.append(e if n == 1 else e.model_copy(update={"id": f"{e.id}#{n}"}))
    return out


def run_dir(input_dir: str | Path, labels_path: str | Path | None = None) -> ReconResult:
    rows = load_from_dir(input_dir)
    labels = None
    if labels_path and Path(labels_path).exists():
        labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    name = Path(input_dir).name
    return run_rows(rows, dataset=name, labels=labels)


def run_bundled(name: str) -> ReconResult:
    rows, labels, truth = load_dataset(name)
    return run_rows(rows, dataset=name, labels=labels or None, truth=truth or None)


def write_outputs(result: ReconResult, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reconciliation.json").write_text(
        result.model_dump_json(indent=2, exclude={"entries"}), encoding="utf-8"
    )
    (out / "metrics.json").write_text(
        result.metrics.model_dump_json(indent=2), encoding="utf-8"
    )
    (out / "audit.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in result.audit) + "\n", encoding="utf-8"
    )
    _exceptions_csv(out / "exceptions.csv", result)


def _exceptions_csv(path: Path, result: ReconResult) -> None:
    lines = ["entry_id,source,amount_inr,value_date,category,confidence,suggested_action"]
    for e in result.exceptions:
        action = e.suggested_action.replace(",", ";")
        lines.append(
            f"{e.entry_id},{e.source},{e.amount_paise/100:.2f},{e.value_date},"
            f"{e.category},{e.confidence},{action}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
