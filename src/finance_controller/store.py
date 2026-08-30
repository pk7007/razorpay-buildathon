"""Persistence: reconciliation runs, the exception queue, and its history.

Why this exists
---------------
Reconciliation *computation* is a pure function of its inputs and stays that way.
But reconciliation *work* is not: an item unmatched in July often matches in
August when the payout finally lands, and a finance team works an exception queue
over days -- assigning, annotating, resolving. An analyzer that forgets everything
at the end of a run cannot support that, which is the difference between a
matcher and a product.

So the split is deliberate:

* the engine stays stateless and reproducible
* the *outcome* is persisted, keyed by a stable fingerprint of the entry itself,
  so re-running the same batch updates the same exception instead of creating a
  second one

SQLite because it needs no service, ships inside the container, and a single
merchant's reconciliation history is measured in megabytes. The schema is small
enough to read in one screen and every table earns its place.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ReconException, ReconResult

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version     INTEGER NOT NULL
);

-- One reconciliation execution. Immutable once written.
CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    dataset           TEXT NOT NULL,
    source_digest     TEXT NOT NULL,          -- fingerprint of the input batch
    started_at        TEXT NOT NULL,
    entries           INTEGER NOT NULL,
    groups            INTEGER NOT NULL,
    exceptions        INTEGER NOT NULL,
    auto_match_rate   REAL    NOT NULL,
    resolver_mode     TEXT    NOT NULL,
    metrics_json      TEXT    NOT NULL,
    money_json        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_digest  ON runs(source_digest);

-- The exception queue. `fingerprint` is what makes re-running idempotent: the
-- same underlying entry maps to the same row, so a second run updates rather
-- than duplicates, and any human work on it survives.
CREATE TABLE IF NOT EXISTS exceptions (
    id                TEXT PRIMARY KEY,
    fingerprint       TEXT NOT NULL UNIQUE,
    entry_id          TEXT NOT NULL,
    source            TEXT NOT NULL,
    category          TEXT NOT NULL,
    amount_minor      INTEGER NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'INR',
    value_date        TEXT NOT NULL,
    confidence        REAL NOT NULL,
    priority          TEXT NOT NULL DEFAULT 'medium',
    status            TEXT NOT NULL DEFAULT 'open',
    assignee          TEXT,
    rationale         TEXT NOT NULL,
    suggested_action  TEXT NOT NULL,
    resolution_reason TEXT,
    related_ids       TEXT NOT NULL DEFAULT '[]',
    first_seen_run    TEXT NOT NULL REFERENCES runs(id),
    last_seen_run     TEXT NOT NULL REFERENCES runs(id),
    times_seen        INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_exc_status   ON exceptions(status);
CREATE INDEX IF NOT EXISTS idx_exc_category ON exceptions(category);
CREATE INDEX IF NOT EXISTS idx_exc_date     ON exceptions(value_date);
CREATE INDEX IF NOT EXISTS idx_exc_amount   ON exceptions(amount_minor);
CREATE INDEX IF NOT EXISTS idx_exc_priority ON exceptions(priority);
CREATE INDEX IF NOT EXISTS idx_exc_assignee ON exceptions(assignee);

-- Append-only history. Every status change, note and auto-resolution lands here,
-- so "why is this closed?" always has an answer.
CREATE TABLE IF NOT EXISTS exception_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_id  TEXT NOT NULL REFERENCES exceptions(id) ON DELETE CASCADE,
    at            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    kind          TEXT NOT NULL,     -- created | status | note | assign | auto_resolved | reopened
    from_status   TEXT,
    to_status     TEXT,
    body          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_exc ON exception_events(exception_id, id);
"""

# An exception moves through this. Anything not listed is refused, so the queue
# cannot reach a state nobody designed.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open":          ("investigating", "resolved", "written_off"),
    "investigating": ("open", "resolved", "written_off"),
    "resolved":      ("open",),          # reopen if it comes back
    "written_off":   ("open",),
}
STATUSES = tuple(TRANSITIONS)
PRIORITIES = ("low", "medium", "high", "critical")

# categories where the money is genuinely at risk get worked first
_HIGH_VALUE_CATEGORIES = {"missing_in_bank", "over_refunded", "orphan_chargeback"}


class WorkflowError(ValueError):
    """An illegal state transition or unknown value."""


def default_db_path() -> Path:
    return Path(os.getenv("RECON_DB_PATH", "data/reconciliation.db"))


class Store:
    """Thin, explicit SQLite wrapper. No ORM -- the schema is small and the
    queries are the interesting part."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(DDL)
        if not self._conn.execute("SELECT 1 FROM schema_meta").fetchone():
            self._conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction. Either the whole run persists or none of it does."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ----------------------------------------------------------------- runs

    def record_run(self, result: ReconResult, source_digest: str) -> str:
        """Persist a run and reconcile the exception queue against it.

        Returns the run id. This is the only write path the engine uses, and it
        is idempotent with respect to ``source_digest``: uploading the same batch
        twice produces a second run row (an audit fact -- it did happen twice)
        but never a duplicate exception.
        """
        run_id = uuid.uuid4().hex[:12]
        now = _now()
        with self._tx() as c:
            c.execute(
                """INSERT INTO runs(id, dataset, source_digest, started_at, entries,
                                    groups, exceptions, auto_match_rate, resolver_mode,
                                    metrics_json, money_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, result.dataset, source_digest, now,
                    result.metrics.total_entries, result.metrics.groups,
                    result.metrics.exceptions, result.metrics.auto_match_rate,
                    result.resolver_mode,
                    result.metrics.model_dump_json(), result.money.model_dump_json(),
                ),
            )
            self._sync_exceptions(c, result, run_id, now)
        return run_id

    def _sync_exceptions(
        self, c: sqlite3.Connection, result: ReconResult, run_id: str, now: str
    ) -> None:
        by_id = {e.id: e for e in result.entries}
        seen: set[str] = set()

        for exc in _queue_items(result):
            entry = by_id.get(exc.entry_id)
            currency = entry.currency if entry else "INR"
            fp = fingerprint(exc.entry_id, exc.category, exc.amount_paise, currency)
            seen.add(fp)
            row = c.execute(
                "SELECT id, status, times_seen FROM exceptions WHERE fingerprint=?", (fp,)
            ).fetchone()
            if row is None:
                exc_id = uuid.uuid4().hex[:12]
                c.execute(
                    """INSERT INTO exceptions(
                        id, fingerprint, entry_id, source, category, amount_minor,
                        currency, value_date, confidence, priority, status, rationale,
                        suggested_action, first_seen_run, last_seen_run, times_seen,
                        created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,1,?,?)""",
                    (
                        exc_id, fp, exc.entry_id, exc.source, exc.category,
                        exc.amount_paise, currency, exc.value_date.isoformat(),
                        exc.confidence, _priority_for(exc.category, exc.amount_paise),
                        exc.rationale, exc.suggested_action, run_id, run_id, now, now,
                    ),
                )
                _log(c, exc_id, "created", now, actor="system",
                     body=f"raised by run {run_id}: {exc.rationale}", to_status="open")
            else:
                # still unresolved after another run -- bump the sighting count so
                # a stubborn item visibly ages instead of looking brand new
                c.execute(
                    """UPDATE exceptions
                          SET last_seen_run=?, times_seen=times_seen+1, updated_at=?,
                              rationale=?, confidence=?
                        WHERE id=?""",
                    (run_id, now, exc.rationale, exc.confidence, row["id"]),
                )
                if row["status"] == "resolved":
                    c.execute(
                        "UPDATE exceptions SET status='open', resolved_at=NULL WHERE id=?",
                        (row["id"],),
                    )
                    _log(c, row["id"], "reopened", now, actor="system",
                         from_status="resolved", to_status="open",
                         body=f"reappeared in run {run_id}")

        # CARRY-FORWARD: an open exception that this run no longer raises has been
        # answered by newer data -- the August payout arrived for the July sale.
        # It is auto-resolved with the run that explains it, not silently dropped.
        still_open = c.execute(
            "SELECT id, fingerprint, entry_id FROM exceptions "
            "WHERE status IN ('open','investigating')"
        ).fetchall()
        current_entry_ids = {e.id for e in result.entries}
        for row in still_open:
            if row["fingerprint"] in seen:
                continue
            # only auto-resolve if this run actually covered that entry; otherwise
            # the entry simply was not in this batch and we know nothing new
            if row["entry_id"] not in current_entry_ids:
                continue
            c.execute(
                """UPDATE exceptions
                      SET status='resolved', resolved_at=?, updated_at=?,
                          resolution_reason='matched by a later reconciliation run',
                          last_seen_run=?
                    WHERE id=?""",
                (now, now, run_id, row["id"]),
            )
            _log(c, row["id"], "auto_resolved", now, actor="system",
                 from_status="open", to_status="resolved",
                 body=f"run {run_id} matched this entry; no longer an exception")

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_run_row(r) for r in rows]

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run_row(row) if row else None

    # ------------------------------------------------------------ exceptions

    def list_exceptions(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        currency: str | None = None,
        min_amount: int | None = None,
        max_amount: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
        sort: str = "amount_minor",
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        where: list[str] = []
        args: list[Any] = []

        def eq(col: str, val: Any) -> None:
            if val:
                where.append(f"{col}=?")
                args.append(val)

        eq("status", status)
        eq("category", category)
        eq("priority", priority)
        eq("assignee", assignee)
        eq("currency", currency)
        def rng(col: str, op: str, val) -> None:
            if val is not None and val != "":
                where.append(f"{col}{op}?")
                args.append(val)

        rng("amount_minor", ">=", min_amount)
        rng("amount_minor", "<=", max_amount)
        rng("value_date", ">=", date_from)
        rng("value_date", "<=", date_to)
        if search:
            where.append("(entry_id LIKE ? OR rationale LIKE ? OR category LIKE ?)")
            args += [f"%{search}%"] * 3

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        # whitelist: these interpolate into SQL, so they can never come from input
        sort_col = sort if sort in {
            "amount_minor", "value_date", "created_at", "updated_at",
            "confidence", "times_seen", "priority", "category", "status",
        } else "amount_minor"
        direction = "ASC" if str(order).lower() == "asc" else "DESC"

        total = self._conn.execute(
            f"SELECT COUNT(*) c FROM exceptions {clause}", args
        ).fetchone()["c"]
        rows = self._conn.execute(
            f"SELECT * FROM exceptions {clause} ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [_exc_row(r) for r in rows],
        }

    def get_exception(self, exc_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM exceptions WHERE id=?", (exc_id,)).fetchone()
        if not row:
            return None
        out = _exc_row(row)
        out["history"] = [
            dict(r) for r in self._conn.execute(
                "SELECT at, actor, kind, from_status, to_status, body "
                "FROM exception_events WHERE exception_id=? ORDER BY id",
                (exc_id,),
            ).fetchall()
        ]
        return out

    def set_status(
        self, exc_id: str, to_status: str, *, actor: str = "user",
        reason: str | None = None,
    ) -> dict:
        row = self._conn.execute(
            "SELECT status FROM exceptions WHERE id=?", (exc_id,)
        ).fetchone()
        if row is None:
            raise WorkflowError(f"no such exception {exc_id}")
        current = row["status"]
        if to_status not in STATUSES:
            raise WorkflowError(f"unknown status {to_status!r}")
        if to_status != current and to_status not in TRANSITIONS[current]:
            raise WorkflowError(
                f"cannot move {current} -> {to_status}; allowed: "
                f"{', '.join(TRANSITIONS[current])}"
            )
        now = _now()
        with self._tx() as c:
            resolved_at = now if to_status in ("resolved", "written_off") else None
            c.execute(
                """UPDATE exceptions
                      SET status=?, updated_at=?, resolved_at=?, resolution_reason=?
                    WHERE id=?""",
                (to_status, now, resolved_at, reason, exc_id),
            )
            _log(c, exc_id, "status", now, actor=actor,
                 from_status=current, to_status=to_status, body=reason)
        return self.get_exception(exc_id)  # type: ignore[return-value]

    def add_note(self, exc_id: str, body: str, *, actor: str = "user") -> dict:
        if not self._conn.execute(
            "SELECT 1 FROM exceptions WHERE id=?", (exc_id,)
        ).fetchone():
            raise WorkflowError(f"no such exception {exc_id}")
        body = (body or "").strip()
        if not body:
            raise WorkflowError("note is empty")
        now = _now()
        with self._tx() as c:
            _log(c, exc_id, "note", now, actor=actor, body=body[:4000])
            c.execute("UPDATE exceptions SET updated_at=? WHERE id=?", (now, exc_id))
        return self.get_exception(exc_id)  # type: ignore[return-value]

    def assign(self, exc_id: str, assignee: str | None, *, actor: str = "user") -> dict:
        if not self._conn.execute(
            "SELECT 1 FROM exceptions WHERE id=?", (exc_id,)
        ).fetchone():
            raise WorkflowError(f"no such exception {exc_id}")
        now = _now()
        with self._tx() as c:
            c.execute(
                "UPDATE exceptions SET assignee=?, updated_at=? WHERE id=?",
                (assignee, now, exc_id),
            )
            _log(c, exc_id, "assign", now, actor=actor,
                 body=f"assigned to {assignee}" if assignee else "unassigned")
        return self.get_exception(exc_id)  # type: ignore[return-value]

    def queue_summary(self) -> dict:
        by_status = {
            r["status"]: r["c"] for r in self._conn.execute(
                "SELECT status, COUNT(*) c FROM exceptions GROUP BY status"
            )
        }
        by_category = {
            r["category"]: r["c"] for r in self._conn.execute(
                "SELECT category, COUNT(*) c FROM exceptions "
                "WHERE status IN ('open','investigating') GROUP BY category "
                "ORDER BY c DESC"
            )
        }
        open_value = self._conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) v FROM exceptions "
            "WHERE status IN ('open','investigating')"
        ).fetchone()["v"]
        aging = self._conn.execute(
            "SELECT COUNT(*) c FROM exceptions "
            "WHERE status IN ('open','investigating') AND times_seen > 1"
        ).fetchone()["c"]
        return {
            "by_status": by_status,
            "by_category": by_category,
            "open_count": by_status.get("open", 0) + by_status.get("investigating", 0),
            "open_value_minor": open_value,
            "carried_forward": aging,
            "total": sum(by_status.values()),
        }


# --------------------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fingerprint(entry_id: str, category: str, amount_minor: int, currency: str) -> str:
    """Stable identity for an exception across runs.

    Deliberately excludes the run id and timestamps: the same unresolved entry,
    for the same reason, is the same piece of work no matter how many times the
    batch is reconciled. Including the amount and currency means a *changed*
    amount correctly becomes a new exception rather than mutating an old one.
    """
    return f"{entry_id}|{category}|{amount_minor}|{currency}"


# A group whose legs do not all tie is still unfinished work, even though its
# members matched each other. "Booked but never paid out" is the single most
# valuable thing in the queue -- it is recoverable money -- so it belongs on the
# worklist, not only in the run report.
_STATUS_TO_CATEGORY: dict[str, str] = {
    "payout_overdue": "missing_in_bank",
    "unbooked_payout": "missing_in_ledger",
    "ambiguous_split": "split_settlement",
    "partial": "unknown",
}

_STATUS_REASON: dict[str, str] = {
    "payout_overdue": "booked and settled, but no bank credit has arrived",
    "unbooked_payout": "money reached the bank but was never recorded in the ledger",
    "ambiguous_split": "paid out in a batch that could not be attributed uniquely",
    "partial": "some legs reconciled, others are missing with no clean explanation",
}


def _queue_items(result: ReconResult) -> list[ReconException]:
    """Everything a human has to look at: unmatched entries AND groups whose
    status says the loop is not actually closed."""
    items = list(result.exceptions)
    by_id = {e.id: e for e in result.entries}
    for g in result.groups:
        cat = _STATUS_TO_CATEGORY.get(g.status)
        if not cat or not g.entry_ids:
            continue
        anchor = sorted(g.entry_ids)[0]
        entry = by_id.get(anchor)
        if entry is None:
            continue
        items.append(
            ReconException(
                entry_id=anchor,
                source=entry.source,
                amount_paise=g.amount_paise or entry.amount_paise,
                value_date=entry.value_date,
                category=cat,  # type: ignore[arg-type]
                confidence=round(g.confidence, 4),
                suggested_action=_ACTION_BY_STATUS.get(g.status, "Investigate."),
                rationale=(
                    f"{g.group_id} ({'+'.join(g.sources)}): "
                    f"{_STATUS_REASON.get(g.status, g.status)}"
                ),
            )
        )
    return items


_ACTION_BY_STATUS: dict[str, str] = {
    "payout_overdue": "Chase the payout with the gateway — this is recoverable revenue.",
    "unbooked_payout": "Book the journal entry for this credit.",
    "ambiguous_split": "Use the settlement recon report to attribute this batch.",
    "partial": "Investigate which leg is missing and why.",
}


def _priority_for(category: str, amount_minor: int) -> str:
    if category in _HIGH_VALUE_CATEGORIES and amount_minor >= 100_000:
        return "critical"
    if category in _HIGH_VALUE_CATEGORIES:
        return "high"
    if amount_minor >= 500_000:
        return "high"
    if category in ("payout_in_transit", "duplicate"):
        return "low"
    return "medium"


def _log(
    c: sqlite3.Connection, exc_id: str, kind: str, at: str, *,
    actor: str = "user", from_status: str | None = None,
    to_status: str | None = None, body: str | None = None,
) -> None:
    c.execute(
        """INSERT INTO exception_events(exception_id, at, actor, kind,
                                        from_status, to_status, body)
           VALUES(?,?,?,?,?,?,?)""",
        (exc_id, at, actor, kind, from_status, to_status, body),
    )


def _exc_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["related_ids"] = json.loads(d.get("related_ids") or "[]")
    return d


def _run_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for key in ("metrics_json", "money_json"):
        try:
            d[key.replace("_json", "")] = json.loads(d.pop(key))
        except (json.JSONDecodeError, TypeError):
            d[key.replace("_json", "")] = {}
    return d
