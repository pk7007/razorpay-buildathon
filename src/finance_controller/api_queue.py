"""Exception-queue, run-history and ingestion routes.

Kept in its own router rather than growing ``api.py``: these endpoints are the
*workflow* surface (stateful, persisted, human-driven), while ``api.py`` holds
the *reconciliation* surface (stateless, computational). The split matches the
architecture and keeps either file readable in one sitting.
"""
from __future__ import annotations

import logging
import time

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


# Free text written into the audit trail. Refused rather than truncated when it
# is too long: a resolution reason cut off mid-sentence still *looks* like a
# complete record months later, which is a worse failure for an auditable log
# than telling the writer to shorten it now.
_LIMITS = {"actor": 60, "assignee": 120, "reason": 1_000, "body": 4_000}


def _text(payload: dict, field: str, default: str | None = None) -> str | None:
    value = payload.get(field, default)
    if value is None:
        return None
    text = str(value)
    cap = _LIMITS[field]
    if len(text) > cap:
        raise HTTPException(
            422,
            f"{field} is {len(text)} characters; the limit is {cap}. Shorten it, or "
            f"put the detail in a note.",
        )
    return text


@router.patch("/api/exceptions/{exc_id}")
def update_exception(exc_id: str, payload: dict) -> dict:
    """Change status and/or assignee. The state machine refuses illegal moves."""
    if not isinstance(payload, dict):
        raise HTTPException(422, "expected an object")
    store = _store()
    actor = _text(payload, "actor", "user") or "user"
    out = None
    try:
        if "assignee" in payload:
            a = _text(payload, "assignee")
            out = store.assign(exc_id, a or None, actor=actor)
        if payload.get("status"):
            out = store.set_status(
                exc_id, str(payload["status"])[:60], actor=actor,
                reason=_text(payload, "reason"),
            )
    except WorkflowError as exc:
        # an illegal transition is the caller's mistake, not a server fault
        raise HTTPException(409, str(exc)) from exc
    if out is None:
        raise HTTPException(422, "nothing to update: send status and/or assignee")
    return out


@router.post("/api/exceptions/{exc_id}/notes")
def add_note(exc_id: str, payload: dict) -> dict:
    payload = payload or {}
    if not isinstance(payload, dict):
        raise HTTPException(422, "expected an object")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise HTTPException(422, "note body is required")
    body = _text(payload, "body")
    try:
        return _store().add_note(
            exc_id, body, actor=_text(payload, "actor", "user") or "user"
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


# A live probe is a network call, and the dashboard asks for status on every
# load, so the answer is cached briefly. Short enough that fixing a bad key and
# reloading shows the truth within a minute.
_PROBE_TTL_SECONDS = 60.0
_probe_cache: dict[str, object] = {"at": 0.0, "reachable": None, "reason": ""}


def _probe_razorpay() -> tuple[bool | None, str]:
    """Can we actually reach Razorpay test mode right now?

    Returns (reachable, reason). ``None`` means "not configured, nothing to
    probe". This exists because *predicting* provenance from the presence of a
    key is not the same as knowing it: a key with a typo is configured, is
    well-formed, and still yields fixtures.
    """
    if not SETTINGS.has_razorpay:
        return None, "no credentials configured"
    now = time.monotonic()
    if now - float(_probe_cache["at"]) < _PROBE_TTL_SECONDS:
        return _probe_cache["reachable"], str(_probe_cache["reason"])  # type: ignore[return-value]
    try:
        fetch_live(count=1)
        reachable, reason = True, "test-mode API answered"
    except RazorpayUnavailable as exc:
        reachable, reason = False, str(exc)
    _probe_cache.update({"at": now, "reachable": reachable, "reason": reason})
    return reachable, reason


@router.get("/api/razorpay/status")
def razorpay_status() -> dict:
    """What data this instance can actually reach. Never leaks the key itself.

    `provenance_if_run` used to be derived from "is a key present?", which meant
    a mistyped key made the dashboard report **Test-mode API** while every run
    quietly produced fixtures. Fixture data labelled as live Razorpay data is
    the single worst thing this endpoint could say, so the answer is now probed
    rather than predicted.
    """
    configured = SETTINGS.has_razorpay
    key = SETTINGS.razorpay_key_id
    reachable, reason = _probe_razorpay()
    live = bool(configured and reachable)

    if not configured:
        note = ("No credentials: /api/reconcile/razorpay serves LOCAL FIXTURES in the "
                "documented Razorpay API response shape. They are not Razorpay data.")
    elif live:
        note = "Razorpay test-mode credentials are configured and the API answered."
    else:
        note = (f"Credentials are set but the test-mode API could not be reached "
                f"({reason}). Runs will serve LOCAL FIXTURES until this is fixed.")

    return {
        "configured": configured,
        "test_mode": key.startswith("rzp_test_") if key else None,
        "key_hint": (key[:12] + "...") if key else None,
        "reachable": reachable,
        "provenance_if_run": "live_test" if live else "fixture",
        "note": note,
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
