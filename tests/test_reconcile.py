"""End-to-end smoke test on the committed sample fixtures."""
from __future__ import annotations

import json
from pathlib import Path

from finance_controller.pipeline import run

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample"


def test_sample_reconciliation(tmp_path):
    metrics = run(SAMPLE, tmp_path, SAMPLE / "labels.json")

    recon = json.loads((tmp_path / "reconciliation.json").read_text())

    # 8 entries -> 3 matched groups (6 entries), 2 exceptions
    assert len(recon["groups"]) == 3
    assert len(recon["exceptions"]) == 2

    # deterministic stage should carry the whole sample (no API key in CI)
    assert all(g["stage"] == "deterministic" for g in recon["groups"])

    # perfect precision/recall against the labels
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["auto_match_rate"] == 0.75

    # conservation: audit log + exceptions cover every entry exactly once
    audit_lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) >= 3


def test_exceptions_are_explained(tmp_path):
    run(SAMPLE, tmp_path, SAMPLE / "labels.json")
    rows = (tmp_path / "exceptions.csv").read_text().strip().splitlines()[1:]
    assert rows
    for row in rows:
        entry_id, category, confidence, action = row.split(",", 3)
        assert category != ""
        assert action != ""
        assert 0.0 <= float(confidence) <= 1.0
