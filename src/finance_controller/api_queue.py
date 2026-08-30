"""Exception-queue, run-history and ingestion routes.

Kept in its own router rather than growing ``api.py``: these endpoints are the
*workflow* surface (stateful, persisted, human-driven), while ``api.py`` holds
the *reconciliation* surface (stateless, computational). The split matches the
architecture and keeps either file readable in one sitting.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile

from .config import SETTINGS
from .mapping import apply_mapping, detect
from .models import ReconResult
from .quality import validate
from .razorpay_source import RazorpayUnavailable, fetch_live, fixture_batch
from .scenarios import ALL as SCENARIOS
from .scenarios import combined as scenarios_combined
from .store import PRIORITIES, STATUSES, WorkflowError

log = logging.getLogger("finance_controller.queue")
router = APIRouter()

_SOURCES = ("payment", "settlement", "bank", "ledger", "refund", "chargeback")
_MAX_FILE_BYTES = 8_000_000

# set by api.py at import time to avoid a circular import
_deps: dict = {}


def wire(*, store_getter, reconcile_and_record, parse_bytes, safe_name, result_exclude):
    _deps.update(
        store=store_getter,
        run=reconcile_and_record,
        parse=parse_bytes,
        safe=safe_name,
        exclude=result_exclude,
    )


def _store():
    return _deps["store"]()


# --------------------------------------------------------------- exception queue


@router.get("/api/exceptions")
def list_exceptions(
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    currency: str | None = None,
    min_amount: int | None = None,
    max_amount: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    sort: str = "amount_minor",
    order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """The persistent queue: filter, sort, search, paginate."""
    if status and status not in STATUSES:
        raise HTTPException(422, f"unknown status {status!r}; expected {list(STATUSES)}")
    if priority and priority not in PRIORITIES:
        raise HTTPException(422, f"unknown priority {priority!r}")
    return _store().list_exceptions(
        status=status, category=category, priority=priority, assignee=assignee,
        currency=currency, min_amount=min_amount, max_amount=max_amount,
        date_from=date_from, date_to=date_to, search=q,
        sort=sort, order=order,
        limit=max(1, min(limit, 500)), offset=max(0, offset),
    )


@router.get("/api/exceptions/summary")
def exceptions_summary() -> dict:
    return _store().queue_summary()


@router.get("/api/exceptions/{exc_id}")
def get_exception(exc_id: str) -> dict:
    exc = _store().get_exception(exc_id)
    if not exc:
        raise HTTPException(404, "no such exception")
    return exc


@router.patch("/api/exceptions/{exc_id}")
def update_exception(exc_id: str, payload: dict) -> dict:
    """Change status and/or assignee. The state machine refuses illegal moves."""
    if not isinstance(payload, dict):
        raise HTTPException(422, "expected an object")
    store = _store()
    actor = str(payload.get("actor") or "user")[:60]
    out = None
    try:
        if "assignee" in payload:
            a = payload["assignee"]
            out = store.assign(exc_id, str(a)[:60] if a else None, actor=actor)
        if payload.get("status"):
            reason = payload.get("reason")
            out = store.set_status(
                exc_id, str(payload["status"]), actor=actor,
                reason=str(reason)[:500] if reason else None,
            )
    except WorkflowError as exc:
        # an illegal transition is the caller's mistake, not a server fault
        raise HTTPException(409, str(exc)) from exc
    if out is None:
        raise HTTPException(422, "nothing to update: send status and/or assignee")
    return out


@router.post("/api/exceptions/{exc_id}/notes")
def add_note(exc_id: str, payload: dict) -> dict:
    body = (payload or {}).get("body")
    if not isinstance(body, str) or not body.strip():
        raise HTTPException(422, "note body is required")
    try:
        return _store().add_note(
            exc_id, body, actor=str((payload or {}).get("actor") or "user")[:60]
        )
    except WorkflowError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/runs")
def list_runs(limit: int = 50) -> list[dict]:
    return _store().list_runs(limit=max(1, min(limit, 200)))


@router.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = _store().get_run(run_id)
    if not run:
        raise HTTPException(404, "no such run")
    return run


# ---------------------------------------------------------------------- sources


@router.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    """The hand-checked financial situations, for the demo and for testing."""
    return [
        {"key": s.key, "title": s.title, "story": s.story} for s in SCENARIOS.values()
    ]


@router.post("/api/reconcile/scenarios", response_model=ReconResult,
             response_model_exclude_none=True)
def reconcile_scenarios() -> ReconResult:
    return _deps["run"](scenarios_combined(), "scenarios")


@router.post("/api/reconcile/razorpay", response_model=ReconResult,
             response_model_exclude_none=True)
def reconcile_razorpay(payload: dict | None = None) -> ReconResult:
    """Reconcile Razorpay data.

    Uses the test-mode API when credentials are configured, otherwise local
    fixtures in the documented API shape. The dataset name records which,
    because fixture data must never be mistaken for live data.
    """
    prefer_live = bool((payload or {}).get("live", True))
    if prefer_live and SETTINGS.has_razorpay:
        try:
            batch = fetch_live()
        except RazorpayUnavailable as exc:
            log.warning("razorpay live pull failed, using fixtures: %s", exc)
            batch = fixture_batch()
    else:
        batch = fixture_batch()
    return _deps["run"](batch.as_rows(), f"razorpay-{batch.provenance}")


@router.get("/api/razorpay/status")
def razorpay_status() -> dict:
    """What data this instance can actually reach. Never leaks the key itself."""
    configured = SETTINGS.has_razorpay
    key = SETTINGS.razorpay_key_id
    return {
        "configured": configured,
        "test_mode": key.startswith("rzp_test_") if key else None,
        "key_hint": (key[:12] + "...") if key else None,
        "provenance_if_run": "live_test" if configured else "fixture",
        "note": (
            "Razorpay test-mode credentials are configured."
            if configured else
            "No credentials: /api/reconcile/razorpay serves LOCAL FIXTURES in the "
            "documented Razorpay API response shape. They are not Razorpay data."
        ),
    }


@router.post("/api/ingest/preview")
async def ingest_preview(source: str, file: UploadFile = File(...)) -> dict:
    """Show how a file's columns map, before committing to a reconciliation.

    This is the answer to "every bank names its columns differently": the caller
    sees the proposed mapping, what was ambiguous, what is missing and how many
    rows would survive validation, and can fix the file rather than discovering
    the problem buried inside a run.
    """
    if source not in _SOURCES:
        raise HTTPException(422, f"unknown source {source!r}; expected {list(_SOURCES)}")
    blob = await file.read()
    if len(blob) > _MAX_FILE_BYTES:
        raise HTTPException(413, f"{_deps['safe'](file.filename)} exceeds 8 MB")
    try:
        rows = _deps["parse"](file.filename or "upload.csv", blob)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            422, f"could not parse {_deps['safe'](file.filename)}: {type(exc).__name__}"
        ) from exc
    if not rows:
        raise HTTPException(422, "no rows found in the file")

    cm = detect(source, list(rows[0].keys()))
    mapped = apply_mapping(cm, rows[:500])
    _, report = validate(source, mapped)
    return {
        "source": source,
        "columns_detected": list(rows[0].keys()),
        "mapping": cm.mapping,
        "split_amount": cm.split_amount,
        "ambiguous": cm.ambiguous,
        "missing_required": cm.missing_required,
        "unmapped_columns": cm.unmapped_columns,
        "usable": cm.ok,
        "explain": cm.explain(),
        "quality": report.summary(),
        "sample": [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in mapped[:3]
        ],
    }
