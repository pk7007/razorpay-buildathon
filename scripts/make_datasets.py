"""Regenerate the bundled benchmark datasets under data/datasets/."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finance_controller.synth import PROFILES, write_dataset  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "datasets"


def main() -> None:
    for profile in PROFILES:
        path = write_dataset(OUT / profile, profile=profile, seed=7)
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
