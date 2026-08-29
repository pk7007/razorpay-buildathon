"""CLI entry point for the AI Finance Controller reconciliation engine.

    # bundled benchmark dataset (clean | realistic | messy)
    python scripts/run_reconciliation.py --dataset realistic --out out/

    # your own exports
    python scripts/run_reconciliation.py --input path/to/dir --out out/ --labels labels.json
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# keep the rupee sign printable on a Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
else:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from finance_controller.pipeline import run_bundled, run_dir, write_outputs  # noqa: E402


def _fmt(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def _print_evaluation() -> None:
    from finance_controller.evaluate import holdout_report

    rep = holdout_report()
    print("\n  Accuracy — rules were tuned on the dev seed; held-out seeds were never seen.\n")
    head = f"  {'':26} {'runs':>5} {'entries':>8} {'precis':>8} {'worst':>8} " \
           f"{'recall':>8} {'F1':>8} {'excCat':>8} {'₹ wrong':>9}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for label, d in (("dev (tuned here)", rep["dev"]), ("held-out (unseen)", rep["holdout"])):
        print(f"  {label:26} {d['runs']:>5} {d['total_entries']:>8,} "
              f"{d['precision_mean']:>8.4f} {d['precision_worst']:>8.4f} "
              f"{d['recall_mean']:>8.4f} {d['f1_mean']:>8.4f} "
              f"{d['exception_category_accuracy_mean']:>8.3f} "
              f"{d['false_match_cost_inr_total']:>9,.0f}")
    g = rep["generalisation_gap"]
    print(f"\n  generalisation gap: F1 {g['f1'] * 100:.2f} pts, recall {g['recall'] * 100:.2f} pts")
    print(f"  all replay-stable: {rep['holdout']['all_replay_stable']}")
    imperfect = [r for r in rep["holdout_runs"] if r["precision"] < 0.999 or r["recall"] < 0.999]
    print(f"\n  imperfect runs: {len(imperfect)} of {len(rep['holdout_runs'])}")
    for r in imperfect:
        print(f"    {r['profile']:10} seed {r['seed']}  "
              f"P={r['precision']:.4f}  R={r['recall']:.4f}")


def _print_benchmark() -> None:
    from finance_controller.evaluate import benchmark

    res = benchmark((1_000, 5_000, 20_000, 50_000))
    print("\n  Throughput — single process, no database.\n")
    print(f"  {'records':>9} {'seconds':>9} {'records/sec':>13} {'auto-match':>11}")
    print("  " + "-" * 45)
    for r in res["runs"]:
        print(f"  {r['records']:>9,} {r['seconds']:>9.2f} "
              f"{r['records_per_sec']:>13,} {r['auto_match_rate']:>11.1%}")
    print(f"\n  peak {res['peak_records_per_sec']:,} records/sec")


def main() -> None:
    ap = argparse.ArgumentParser(description="AI Finance Controller — reconciliation run")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dataset", choices=["clean", "realistic", "messy"],
                     help="a bundled benchmark dataset with a ground-truth answer key")
    src.add_argument("--input", help="directory containing payments/settlements/bank/ledger files")
    src.add_argument("--evaluate", action="store_true",
                     help="dev vs held-out accuracy across unseen seeds")
    src.add_argument("--benchmark", action="store_true",
                     help="throughput at increasing batch sizes")
    ap.add_argument("--labels", default=None, help="labels.json for metrics (with --input)")
    ap.add_argument("--out", default="out", help="output directory")
    args = ap.parse_args()

    if args.evaluate:
        return _print_evaluation()
    if args.benchmark:
        return _print_benchmark()

    result = run_bundled(args.dataset) if args.dataset else run_dir(args.input, args.labels)
    write_outputs(result, args.out)

    m, mo = result.metrics, result.money
    print(f"\n  dataset            {result.dataset}   ({result.resolver_mode} resolver)")
    print(f"  entries            {m.total_entries}")
    print(f"  auto-match rate    {m.auto_match_rate:.1%}   ({m.matched_entries} matched, "
          f"{m.exceptions} exceptions)")
    if m.precision is not None:
        print(f"  precision / recall {m.precision:.3f} / {m.recall:.3f}   F1 {m.f1:.3f}")
    if m.exception_category_accuracy is not None:
        print(f"  exception accuracy {m.exception_category_accuracy:.1%}")
    print(f"  replay stable      {m.replay_stable}")
    print(f"  latency            {m.latency_ms} ms")
    if m.llm_calls:
        print(f"  llm                {m.llm_calls} call(s), {m.llm_input_tokens}+"
              f"{m.llm_output_tokens} tok, ${m.llm_cost_usd:.4f}")
    print()
    print(f"  reconciled         {_fmt(mo.reconciled_paise)}")
    print(f"  in transit         {_fmt(mo.in_transit_paise)}")
    print(f"  recoverable        {_fmt(mo.recoverable_paise)}   <- chase this")
    print(f"  unrecorded income  {_fmt(mo.unrecorded_paise)}")
    print()
    print(f"  wrote  {Path(args.out) / 'reconciliation.json'}")
    print(f"         {Path(args.out) / 'exceptions.csv'}")
    print(f"         {Path(args.out) / 'audit.jsonl'}")
    print(f"         {Path(args.out) / 'metrics.json'}")


if __name__ == "__main__":
    main()
