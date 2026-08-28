"""In-memory audit trail. One record per reconciliation decision, in order."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .models import AuditRecord, Stage


class AuditLog:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def record(
        self,
        *,
        stage: Stage,
        rule: str,
        inputs: list[str],
        outcome: str,
        confidence: float,
        rationale: str,
    ) -> None:
        self._records.append(
            AuditRecord(
                seq=len(self._records) + 1,
                ts=datetime.now(UTC),
                stage=stage,
                rule=rule,
                inputs=sorted(inputs),
                outcome=outcome,  # type: ignore[arg-type]
                confidence=round(confidence, 4),
                rationale=rationale,
            )
        )

    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def write_jsonl(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "\n".join(r.model_dump_json() for r in self._records) + "\n", encoding="utf-8"
        )
