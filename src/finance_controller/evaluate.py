"""Held-out evaluation and throughput benchmarking.

Why this module exists
----------------------
The matching rules in ``reconcile.py`` were developed while looking at the three
bundled datasets (``clean`` / ``realistic`` / ``messy``, all seed 7). Scoring the
matcher on those same datasets is grading your own homework: it measures how well
the rules were fitted, not whether they generalise.

So the datasets are split the way an ML benchmark is:

  DEV_SEED  (7)              — the seed the rules were tuned against. Reportable,
                               but NOT evidence of generalisation.
  HOLDOUT_SEEDS (101..105)   — never looked at while writing a rule. These are the
                               numbers that count, and they are reported as-is,
                               including when they are worse.

``holdout_report()`` is what the README, the API and the CLI all quote.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from .pipeline import run_rows
from .synth import PROFILES, generate

DEV_SEED = 7
HOLDOUT_SEEDS = (101, 102, 103, 104, 105)


@dataclass
class Score:
    """One reconciliation run scored against its ground-truth key."""

    profile: str
    seed: int
    entries: int
    groups: int
    exceptions: int
    auto_match_rate: float
    precision: float
    recall: float
    f1: float
    exception_category_accuracy: float | None
    resolver_share: float
    replay_stable: bool
    latency_ms: int
    # money-weighted correctness: what fraction of rupees sat in a correct group
    value_precision: float = 0.0
    false_match_cost_paise: int = 0

    def as_row(self) -> dict:
        return {
            "profile": self.profile,
            "seed": self.seed,
            "entries": self.entries,
            "auto_match_rate": self.auto_match_rate,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "exception_category_accuracy": self.exception_category_accuracy,
            "value_precision": self.value_precision,
            "false_match_cost_inr": round(self.false_match_cost_paise / 100, 2),
            "resolver_share": self.resolver_share,
            "replay_stable": self.replay_stable,
            "latency_ms": self.latency_ms,
        }


@dataclass
class Aggregate:
    label: str
    scores: list[Score] = field(default_factory=list)

    def _mean(self, attr: str) -> float:
        vals = [getattr(s, attr) for s in self.scores if getattr(s, attr) is not None]
        return round(statistics.fmean(vals), 4) if vals else 0.0

    def _min(self, attr: str) -> float:
        vals = [getattr(s, attr) for s in self.scores if getattr(s, attr) is not None]
        return round(min(vals), 4) if vals else 0.0

    def summary(self) -> dict:
        return {
            "label": self.label,
            "runs": len(self.scores),
            "total_entries": sum(s.entries for s in self.scores),
            "precision_mean": self._mean("precision"),
            "precision_worst": self._min("precision"),
            "recall_mean": self._mean("recall"),
            "recall_worst": self._min("recall"),
            "f1_mean": self._mean("f1"),
            "auto_match_rate_mean": self._mean("auto_match_rate"),
            "exception_category_accuracy_mean": self._mean("exception_category_accuracy"),
            "value_precision_mean": self._mean("value_precision"),
            "false_match_cost_inr_total": round(
                sum(s.false_match_cost_paise for s in self.scores) / 100, 2
            ),
            "resolver_share_mean": self._mean("resolver_share"),
            "all_replay_stable": all(s.replay_stable for s in self.scores),
        }


def score_run(profile: str, seed: int) -> Score:
    """Generate a dataset at (profile, seed), reconcile it, score against its key."""
    data = generate(profile, seed)
    rows = {
        "payment": data["payments"],
        "settlement": data["settlements"],
        "bank": data["bank"],
        "ledger": data["ledger"],
    }
    result = run_rows(
        rows,
        dataset=f"{profile}@{seed}",
        labels=data["labels"] or None,
        truth=data["truth"] or None,
    )
    m = result.metrics
    vp, cost = _value_weighted(result, data["labels"])
    return Score(
        profile=profile,
        seed=seed,
        entries=m.total_entries,
        groups=m.groups,
        exceptions=m.exceptions,
        auto_match_rate=m.auto_match_rate,
        precision=m.precision if m.precision is not None else 0.0,
        recall=m.recall if m.recall is not None else 0.0,
        f1=m.f1 if m.f1 is not None else 0.0,
        exception_category_accuracy=m.exception_category_accuracy,
        resolver_share=m.resolver_share,
        replay_stable=m.replay_stable,
        latency_ms=m.latency_ms,
        value_precision=vp,
        false_match_cost_paise=cost,
    )


def _value_weighted(result, labels: dict[str, list[str]]) -> tuple[float, int]:
    """Money-weighted precision + the rupee cost of wrong auto-matches.

    A wrong match is far more expensive than an exception: an exception gets a
    human's attention, a wrong match silently closes the book on real money. So
    the rupee value sitting inside incorrect groups is reported separately.
    """
    if not labels:
        return 1.0, 0
    truth_group_of: dict[str, str] = {}
    for gid, ids in labels.items():
        for i in ids:
            truth_group_of[i] = gid

    good = bad = 0
    for g in result.groups:
        # a group is "correct" if every member belongs to the same true group
        seen = {truth_group_of.get(i) for i in g.entry_ids}
        if len(seen) == 1 and None not in seen:
            good += g.amount_paise
        else:
            bad += g.amount_paise
    total = good + bad
    return (round(good / total, 4) if total else 1.0), bad


def evaluate(seeds: tuple[int, ...], profiles: tuple[str, ...] | None = None) -> Aggregate:
    profiles = profiles or tuple(PROFILES)
    label = "dev (tuned on)" if seeds == (DEV_SEED,) else f"held-out seeds {list(seeds)}"
    agg = Aggregate(label=label)
    for seed in seeds:
        for profile in profiles:
            agg.scores.append(score_run(profile, seed))
    return agg


def holdout_report() -> dict:
    """The full dev-vs-held-out comparison. This is what gets published."""
    dev = evaluate((DEV_SEED,))
    hold = evaluate(HOLDOUT_SEEDS)
    d, h = dev.summary(), hold.summary()
    return {
        "dev": d,
        "holdout": h,
        "generalisation_gap": {
            "precision": round(d["precision_mean"] - h["precision_mean"], 4),
            "recall": round(d["recall_mean"] - h["recall_mean"], 4),
            "f1": round(d["f1_mean"] - h["f1_mean"], 4),
        },
        "dev_runs": [s.as_row() for s in dev.scores],
        "holdout_runs": [s.as_row() for s in hold.scores],
    }


# --------------------------------------------------------------------------- throughput


def benchmark(sizes: tuple[int, ...] = (1_000, 10_000, 50_000)) -> dict:
    """Scale the ``realistic`` profile up and measure records/sec.

    Row counts are approximate: the generator is driven by days x flows-per-day,
    so the target is hit by scaling the number of days.
    """
    runs = []
    for target in sizes:
        rows, entries = _synth_at_scale(target)
        t0 = time.perf_counter()
        result = run_rows(rows, dataset=f"bench-{target}", check_replay=False)
        elapsed = time.perf_counter() - t0
        runs.append(
            {
                "target_records": target,
                "records": result.metrics.total_entries,
                "seconds": round(elapsed, 3),
                "records_per_sec": int(result.metrics.total_entries / elapsed) if elapsed else 0,
                "auto_match_rate": result.metrics.auto_match_rate,
                "groups": result.metrics.groups,
                "exceptions": result.metrics.exceptions,
            }
        )
    return {"runs": runs, "peak_records_per_sec": max(r["records_per_sec"] for r in runs)}


def _synth_at_scale(target_records: int) -> tuple[dict, int]:
    """Build a rows dict of roughly ``target_records`` entries by stacking months."""
    from .synth import PROFILES as _P

    per_day = 7 * 4  # ~7 clean flows/day x 4 sources
    days = max(2, target_records // per_day)
    base = _P["realistic"]
    scaled = type(base)(
        name="scaled",
        days=days,
        happy_per_day=base.happy_per_day,
        split_batches=max(1, days // 4),
        merged_payouts=max(1, days // 5),
        duplicates=max(1, days // 5),
        missing_in_bank=max(1, days // 5),
        missing_in_ledger=max(1, days // 5),
        payout_in_transit=max(1, days // 6),
        bank_charge_cases=max(1, days // 5),
        off_gateway_transfers=max(1, days // 6),
    )
    _P["scaled"] = scaled
    try:
        data = generate("scaled", seed=99)
    finally:
        _P.pop("scaled", None)
    rows = {
        "payment": data["payments"],
        "settlement": data["settlements"],
        "bank": data["bank"],
        "ledger": data["ledger"],
    }
    return rows, sum(len(v) for v in rows.values())
