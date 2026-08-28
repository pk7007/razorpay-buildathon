"""Loaders: local CSV/JSON files, and Razorpay test-mode API pulls."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .config import SETTINGS
from .models import Source

_FILES: dict[Source, tuple[str, ...]] = {
    "payment": ("payments.csv", "payments.json"),
    "settlement": ("settlements.csv", "settlements.json"),
    "bank": ("bank.csv", "bank_statement.csv", "bank.json"),
    "ledger": ("ledger.csv", "ledger.json"),
}


def _read_file(path: Path) -> list[dict]:
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("items", [])
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_from_dir(input_dir: str | Path) -> dict[Source, list[dict]]:
    base = Path(input_dir)
    result: dict[Source, list[dict]] = {}
    for source, names in _FILES.items():
        for name in names:
            p = base / name
            if p.exists():
                result[source] = _read_file(p)
                break
        else:
            result[source] = []
    return result


def load_from_razorpay(year: int, month: int) -> dict[Source, list[dict]]:
    """Pull payments + settlements for a month from Razorpay TEST mode.

    Bank + ledger have no Razorpay source; supply those as files.
    """
    import razorpay  # local import so the package imports without the dep at rest

    client = razorpay.Client(auth=(SETTINGS.razorpay_key_id, SETTINGS.razorpay_key_secret))
    # NOTE: fill in from/to epoch bounds for the month; kept explicit on purpose.
    payments = client.payment.all({"count": 100})["items"]
    settlements = client.settlement.all({"count": 100})["items"]
    return {"payment": payments, "settlement": settlements, "bank": [], "ledger": []}
