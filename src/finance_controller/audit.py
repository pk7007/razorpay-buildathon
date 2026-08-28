"""Append-only audit log. One JSON record per decision."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import AuditRecord


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # truncate at start of a run; the file is a full record of THIS run
        self.path.write_text("", encoding="utf-8")

    def record(
        self,
        *,
        stage: str,
        rule: str,
        inputs: list[str],
        outcome: str,
        confidence: float,
        rationale: str,
    ) -> None:
        rec = AuditRecord(
            ts=datetime.now(timezone.utc),
            stage=stage,  # type: ignore[arg-type]
            rule=rule,
            inputs=inputs,
            outcome=outcome,  # type: ignore[arg-type]
            confidence=confidence,
            rationale=rationale,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(rec.model_dump_json() + "\n")
