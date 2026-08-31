"""Loaders: local CSV/JSON files, bundled benchmark datasets, Razorpay test-mode API."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from .config import SETTINGS
from .mapping import apply_mapping, detect
from .models import Source
from .quality import validate

_FILES: dict[Source, tuple[str, ...]] = {
    "payment": ("payments.csv", "payments.json"),
    "settlement": ("settlements.csv", "settlements.json"),
    "bank": ("bank.csv", "bank_statement.csv", "bank.json"),
    "ledger": ("ledger.csv", "books.csv", "ledger.json"),
    # Deductions are optional in a bundled dataset -- most months have none --
    # but they are first-class sources when the files are there.
    "refund": ("refunds.csv", "refunds.json"),
    "chargeback": ("chargebacks.csv", "chargebacks.json", "disputes.csv"),
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


class IngestRefused(ValueError):
    """A file could not be mapped, and guessing would corrupt the result."""


def prepare(source: Source, parsed: list[dict], label: str = "") -> list[dict]:
    """Detect columns, refuse rather than guess, then keep the rows that validate.

    Every path into the engine goes through this function -- an upload, a
    bundled dataset, a directory on disk. It used to live only in the upload
    endpoint, so a dataset directory whose CSVs used real-world headers was fed
    to the engine raw: every amount normalised to zero and every date to the
    epoch, and the engine then "reconciled" rows worth nothing. One door, so
    that cannot happen again on the next path someone adds.
    """
    if not parsed:
        return []
    name = label or source
    cm = detect(source, list(parsed[0].keys()))
    if cm.ambiguous:
        which = "; ".join(f"{f}: {' / '.join(cols)}" for f, cols in cm.ambiguous.items())
        raise IngestRefused(
            f"{name}: two columns are equally plausible for the same field ({which}). "
            "Rename one so the mapping is unambiguous -- guessing here would corrupt "
            "the reconciliation silently."
        )
    if cm.missing_required:
        raise IngestRefused(
            f"{name}: no column could be mapped to {', '.join(cm.missing_required)}. "
            "Add or rename that column."
        )
    mapped = apply_mapping(cm, parsed)
    good, report = validate(source, mapped)
    if not good and report.total_rows:
        raise IngestRefused(f"{name}: no row passed validation ({report.total_rows} read)")
    return good


def load_from_dir(input_dir: str | Path, *, mapped: bool = True) -> dict[Source, list[dict]]:
    """Read a dataset directory. ``mapped=False`` returns the raw parsed rows,
    which only the column-detection tests have a reason to want."""
    base = Path(input_dir)
    result: dict[Source, list[dict]] = {}
    for source, names in _FILES.items():
        result[source] = []
        for name in names:
            p = base / name
            if p.exists():
                rows = _read_file(p)
                result[source] = prepare(source, rows, p.name) if mapped else rows
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
