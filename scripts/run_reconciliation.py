"""CLI entry point.

    python scripts/run_reconciliation.py --input data/sample --out out/ \
        --labels data/sample/labels.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finance_controller.pipeline import run  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="AI Finance Controller reconciliation run")
    ap.add_argument("--input", required=True, help="dir with payments/settlements/bank/ledger files")
    ap.add_argument("--out", default="out", help="output directory")
    ap.add_argument("--labels", default=None, help="optional labels.json for metrics")
    args = ap.parse_args()

    metrics = run(args.input, args.out, args.labels)
    print(json.dumps(metrics, indent=2))
    print(f"\nwrote: {Path(args.out) / 'reconciliation.json'}")
    print(f"       {Path(args.out) / 'exceptions.csv'}")
    print(f"       {Path(args.out) / 'audit.jsonl'}")
    print(f"       {Path(args.out) / 'metrics.json'}")


if __name__ == "__main__":
    main()
