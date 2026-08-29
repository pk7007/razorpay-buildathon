"""Loaders: local CSV/JSON files, bundled benchmark datasets, Razorpay test-mode API."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from .config import SETTINGS
from .models import Source

_FILES: dict[Source, tuple[str, ...]] = {
    "payment": ("payments.csv", "payments.json"),
    "settlement": ("settlements.csv", "settlements.json"),
    "bank": ("bank.csv", "bank_statement.csv", "bank.json"),
    "ledger": ("ledger.csv", "books.csv", "ledger.json"),
}

_REPO_ROOT = Path(__file__).resolve().parents[2]
DATASETS_DIR = _REPO_ROOT / "data" / "datasets"


def parse_bytes(name: str, blob: bytes) -> list[dict]:
    text = blob.decode("utf-8-sig", errors="replace")
    if name.lower().endswith(".json"):
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("items", data.get("rows", []))
    return list(csv.DictReader(io.StringIO(text)))


def _read_file(path: Path) -> list[dict]:
    return parse_bytes(path.name, path.read_bytes())


def load_from_dir(input_dir: str | Path) -> dict[Source, list[dict]]:
    base = Path(input_dir)
    result: dict[Source, list[dict]] = {}
    for source, names in _FILES.items():
        result[source] = []
        for name in names:
            p = base / name
            if p.exists():
                result[source] = _read_file(p)
                break
    return result


def available_datasets() -> list[str]:
    if not DATASETS_DIR.exists():
        return []
    return sorted(p.name for p in DATASETS_DIR.iterdir() if p.is_dir())


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def load_dataset(name: str) -> tuple[dict[Source, list[dict]], dict, dict]:
    """Return (rows_by_source, labels, truth) for a bundled benchmark dataset.

    The API only ever passes a name from ``available_datasets()``, but this is a
    public function that joins its argument onto a path, so it validates the name
    itself rather than trusting every future caller.
    """
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        raise ValueError(f"invalid dataset name {name!r}")
    d = DATASETS_DIR / name
    if not d.is_dir() or d.resolve().parent != DATASETS_DIR.resolve():
        raise FileNotFoundError(f"dataset {name!r} not found in {DATASETS_DIR}")
    rows = load_from_dir(d)
    labels = _maybe_json(d / "labels.json")
    truth = _maybe_json(d / "truth.json")
    return rows, labels, truth


def _maybe_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_from_razorpay(count: int = 100) -> dict[Source, list[dict]]:
    """Pull payments + settlements from Razorpay TEST mode.

    Bank + ledger have no Razorpay source; supply those as files.
    """
    if not SETTINGS.has_razorpay:
        raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set")
    import razorpay

    client = razorpay.Client(auth=(SETTINGS.razorpay_key_id, SETTINGS.razorpay_key_secret))
    payments = client.payment.all({"count": count}).get("items", [])
    settlements = client.settlement.all({"count": count}).get("items", [])
    return {"payment": payments, "settlement": settlements, "bank": [], "ledger": []}
