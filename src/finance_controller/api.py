"""FastAPI app: reconciliation as a service + the dashboard.

    uvicorn finance_controller.api:app --reload

There is deliberately no database and no auth. A reconciliation is a pure
function of the four batches it is given: persisting it would add a breach
surface and a consistency problem while adding nothing to accuracy. Everything
below is therefore stateless, and the response is the only copy of a result.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import SETTINGS
from .evaluate import benchmark, holdout_report
from .ingest import IngestRefused, available_datasets, parse_bytes, prepare
from .models import ReconResult
from .pipeline import run_bundled, run_rows
from .store import Store
from .synth import PROFILES

_WEB = Path(__file__).resolve().parents[2] / "web"

# Python's mimetypes table predates woff2 on some platforms; without this the
# self-hosted fonts are served as application/octet-stream and silently ignored.
mimetypes.add_type("font/woff2", ".woff2")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
log = logging.getLogger("finance_controller")

# cached because both are pure and expensive enough to matter on a demo box
_CACHE: dict[str, object] = {}
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_GUARD = threading.Lock()


def _cached(key: str, produce):
    """Compute once, however many callers arrive at once.

    `/api/evaluation` runs fifteen reconciliations and `/api/benchmark` runs
    three large ones. Both were memoised with a bare `if key not in _CACHE`,
    which is not a cache on a cold instance -- it is a stampede: four concurrent
    first requests each paid the full cost (measured at 3.7s wall against 0.7s
    for one), and this endpoint needs no credentials to call.

    FastAPI runs sync endpoints in a threadpool, so a threading.Lock is the right
    primitive. The double check means the fast path stays lock-free once warm.
    """
    if key in _CACHE:
        return _CACHE[key]
    with _CACHE_GUARD:
        lock = _CACHE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        if key not in _CACHE:                       # someone else may have won
            _CACHE[key] = produce()
    return _CACHE[key]

_STORE: Store | None = None


def get_store() -> Store:
    """One process-wide store. SQLite handles the concurrency we have."""
    global _STORE
    if _STORE is None:
        _STORE = Store()
    return _STORE


def _digest(rows: dict) -> str:
    """Fingerprint of an input batch, so an identical re-upload is recognisable."""
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _reconcile_and_record(rows: dict, dataset: str, **kw) -> ReconResult:
    """Run the engine, then fold the outcome into the persistent queue."""
    result = run_rows(rows, dataset=dataset, **kw)
    try:
        get_store().record_run(result, _digest(rows))
    except Exception:  # noqa: BLE001 - a reporting failure must not lose the result
        log.exception("could not persist run for %s", dataset)
    return result


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # pay the import / model-build cost once so the first real request is fast
    try:
        run_rows({"payment": [], "settlement": [], "bank": [], "ledger": []}, dataset="warmup")
        for name in available_datasets():
            run_bundled(name)
            break
        log.info("warmup complete; resolver=%s", "llm" if SETTINGS.has_llm else "heuristic")
    except Exception:  # noqa: BLE001 - warmup is best-effort
        log.warning("warmup failed; first request will be slower", exc_info=True)
    yield


app = FastAPI(
    title="AI Finance Controller",
    version="1.0.0",
    description="Deterministic-first reconciliation across payments, settlements, bank and ledger.",
    lifespan=_lifespan,
)
# results are large, highly repetitive JSON — gzip roughly 10x's them
app.add_middleware(GZipMiddleware, minimum_size=1024)


# --------------------------------------------------------------------------- limits

_MAX_FILE_BYTES = 8_000_000
_MAX_JSON_BYTES = 1_000_000
# 50k rows across all six files. The reconciliation runs synchronously inside
# the request -- there is no job queue and no background worker, because the
# whole product is one process with no infrastructure to configure -- so the cap
# is set by what finishes inside a normal proxy timeout rather than by what the
# engine can eventually chew through. Measured on the build machine: 50k rows
# lands at ~13s, 100k at ~35s, which is past Render's 30s default and would show
# a merchant a 502 while the server was still working. One merchant-month is
# typically a few thousand rows per source.
_MAX_ROWS = 50_000
_RATE_LIMIT = 30           # requests
_RATE_WINDOW = 60.0        # seconds
_hits: dict[str, deque[float]] = {}
_EXPENSIVE_READS = frozenset({"/api/evaluation", "/api/benchmark"})


def _rate_limited(client: str) -> bool:
    """Fixed-window-per-client limiter.

    In-process on purpose: this service holds no state and is meant to run as a
    single demo instance, so a Redis dependency would buy nothing. Behind more
    than one replica this becomes per-replica, which is documented, not hidden.
    """
    now = time.monotonic()
    seen = _hits.setdefault(client, deque())
    while seen and now - seen[0] > _RATE_WINDOW:
        seen.popleft()
    if len(seen) >= _RATE_LIMIT:
        return True
    seen.append(now)
    return False


@app.middleware("http")
async def _observability(request: Request, call_next):
    """Request id, timing, structured log line, and a last-resort error handler."""
    rid = uuid.uuid4().hex[:8]
    started = time.perf_counter()

    # A JSON body has no size limit of its own -- only uploads were capped -- so
    # a single POST could ask the server to buffer an arbitrary amount of memory
    # before any handler saw it. Multipart is excluded here because the upload
    # endpoint enforces its own, larger, per-file cap.
    if request.method in ("POST", "PATCH", "PUT"):
        declared = request.headers.get("content-length")
        content_type = request.headers.get("content-type", "")
        cap = _MAX_FILE_BYTES * 6 if "multipart/" in content_type else _MAX_JSON_BYTES
        if declared and declared.isdigit() and int(declared) > cap:
            return JSONResponse(
                {"detail": f"request body exceeds {cap // 1_000_000} MB",
                 "request_id": rid},
                status_code=413,
            )

    # Every POST does real work, and so do two GETs: /api/evaluation reruns the
    # held-out sweep and /api/benchmark reconciles 26,000 records. Limiting only
    # POST left the two most expensive endpoints in the service open to anyone
    # who could send a GET.
    costly = (
        request.method == "POST"
        or request.url.path in _EXPENSIVE_READS
    )
    if request.url.path.startswith("/api/") and costly:
        client = request.client.host if request.client else "unknown"
        if _rate_limited(client):
            log.warning("rid=%s rate-limited client=%s", rid, client)
            return JSONResponse(
                {"detail": "rate limit exceeded, try again shortly", "request_id": rid},
                status_code=429,
                headers={"Retry-After": "60"},
            )
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - never leak a stack trace to a client
        log.exception("rid=%s unhandled error on %s", rid, request.url.path)
        return JSONResponse(
            {"detail": "internal error", "request_id": rid}, status_code=500
        )
    took = (time.perf_counter() - started) * 1000
    if request.url.path.startswith("/api/"):
        log.info("rid=%s %s %s -> %s in %.0fms",
                 rid, request.method, request.url.path, response.status_code, took)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Response-Time-ms"] = f"{took:.0f}"
    # static, self-hosted, no third-party anything: lock the page down
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        # script-src stays strict — that is the directive that actually stops XSS.
        # style-src allows inline because the dashboard sets bar widths from data
        # (el.style.width); an injected style cannot execute, only mis-paint.
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'; object-src 'none'",
    )
    if not request.url.path.startswith("/api/"):
        # assets are a few KB and change with every deploy; a stale app.js after a
        # redeploy is a silent, confusing failure, so always revalidate
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


_DATASET_BLURB = {
    "demo": "Nine designed cases in real export formats — zero-MDR UPI, a ₹1.25cr "
    "payout with TDS, a partial refund, a lost chargeback, a payout that crossed "
    "the month end, and four things that should not reconcile.",
    "clean": "Well-behaved month — fees, GST, a T+2 payout cycle, one batch, "
    "one payout in transit.",
    "realistic": "A normal month: split batches, merged payouts, a double-booked entry, "
    "an unrecorded credit, a bank charge, and revenue that never settled.",
    "messy": "A rough month — every anomaly, more often. The stress test.",
}
_ORDER = ["demo", "clean", "realistic", "messy"]


# --------------------------------------------------------------------------- routes


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "resolver": "llm" if SETTINGS.has_llm else "heuristic",
        "razorpay_configured": SETTINGS.has_razorpay,
        "datasets": available_datasets(),
    }


@app.get("/api/datasets")
def datasets() -> list[dict]:
    names = available_datasets()
    ordered = [n for n in _ORDER if n in names] + [n for n in names if n not in _ORDER]
    out = []
    for name in ordered:
        prof = PROFILES.get(name)
        out.append(
            {
                "name": name,
                "label": name.capitalize(),
                "blurb": _DATASET_BLURB.get(name, ""),
                "days": prof.days if prof else None,
            }
        )
    return out


# keep responses lean and don't echo raw uploaded rows back over the wire
_RESULT_EXCLUDE = {"entries": {"__all__": {"raw"}}}


@app.post(
    "/api/reconcile",
    response_model=ReconResult,
    response_model_exclude_none=True,
    response_model_exclude=_RESULT_EXCLUDE,
)
def reconcile_dataset(payload: dict) -> ReconResult:
    name = (payload or {}).get("dataset")
    if not isinstance(name, str) or name not in available_datasets():
        raise HTTPException(404, f"unknown dataset {name!r}")
    rows, labels, truth = _bundled_rows(name)
    return _reconcile_and_record(rows, name, labels=labels or None, truth=truth or None)


@app.post(
    "/api/reconcile/upload",
    response_model=ReconResult,
    response_model_exclude_none=True,
    response_model_exclude=_RESULT_EXCLUDE,
)
async def reconcile_upload(
    payments: UploadFile | None = File(None),
    settlements: UploadFile | None = File(None),
    bank: UploadFile | None = File(None),
    ledger: UploadFile | None = File(None),
    refunds: UploadFile | None = File(None),
    chargebacks: UploadFile | None = File(None),
) -> ReconResult:
    # Six sources, not four. The engine has always modelled refunds and
    # chargebacks as first-class financial events, but the upload path accepted
    # only the four "positive" sources -- so a merchant bringing their own
    # exports could not supply the deductions, and every refunded sale in their
    # data looked like a settlement that came up short.
    files = {
        "payment": payments, "settlement": settlements,
        "bank": bank, "ledger": ledger,
        "refund": refunds, "chargeback": chargebacks,
    }
    if not any(files.values()):
        raise HTTPException(400, "upload at least one file")

    # Two passes. The first only parses and counts, because the row cap has to be
    # decided BEFORE the expensive work: column detection, mapping and validation
    # are O(rows x columns), and doing them on a 100k-row file only to reject it
    # afterwards hands an attacker a cheap way to burn CPU. Counting *raw* rows
    # rather than surviving ones closes the same hole from the other side -- a
    # file of mostly-invalid rows used to slip under a cap that counted only the
    # rows that passed.
    parsed_by_source: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    raw_total = 0
    for source, up in files.items():
        if up is None:
            parsed_by_source[source] = []
            continue
        name = _safe_name(up.filename)
        names[source] = name
        blob = await up.read()
        if len(blob) > _MAX_FILE_BYTES:
            raise HTTPException(413, f"{name} exceeds 8 MB")
        try:
            parsed = parse_bytes(up.filename or f"{source}.csv", blob)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                422, f"could not parse {name}: {type(exc).__name__}"
            ) from exc
        if not isinstance(parsed, list):
            raise HTTPException(422, f"{name} is not a list of rows")
        raw_total += len(parsed)
        if raw_total > _MAX_ROWS:
            raise HTTPException(
                413,
                f"{raw_total} rows exceeds the {_MAX_ROWS}-row limit. Split the "
                f"file by period and reconcile one batch at a time.",
            )
        parsed_by_source[source] = parsed

    # Second pass, now that the size is known to be sane. Uploaded files carry
    # whatever column names the bank chose, so they go through the same
    # detect -> map -> validate path that /api/ingest/preview shows the caller.
    # Skipping it here would make the preview a promise the run does not keep.
    rows: dict = {}
    total = 0
    for source, parsed in parsed_by_source.items():
        rows[source] = _map_and_validate(source, parsed, names.get(source, source))
        total += len(rows[source])
    if total == 0:
        raise HTTPException(422, "no usable rows found in the uploaded files")
    return _reconcile_and_record(rows, "upload")


def _map_and_validate(source: str, parsed: list[dict], name: str) -> list[dict]:
    """Detect the file's columns, refuse rather than guess, then keep the good rows.

    The work lives in ``ingest.prepare`` so the upload path and the bundled
    dataset path cannot drift apart; this wrapper only turns a refusal into the
    422 an HTTP caller expects.
    """
    try:
        return prepare(source, parsed, name)
    except IngestRefused as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/evaluation")
def evaluation() -> dict:
    """Dev-vs-held-out accuracy. The numbers the README quotes, served live so a
    reader can check the claim against the running code."""
    return _cached("evaluation", holdout_report)  # type: ignore[return-value]


@app.get("/api/benchmark")
def throughput() -> dict:
    """Throughput at increasing batch sizes — the other half of the Track 4 bar."""
    return _cached(  # type: ignore[return-value]
        "benchmark", lambda: benchmark((1_000, 5_000, 20_000))
    )


def _bundled_rows(name: str):
    """(rows, labels, truth) for a bundled benchmark dataset."""
    from .ingest import load_dataset

    return load_dataset(name)


def _safe_name(name: str | None) -> str:
    """Never echo a raw client-supplied filename back into a response body."""
    if not name:
        return "file"
    return "".join(c for c in Path(name).name if c.isalnum() or c in "._-")[:60] or "file"


from . import api_queue  # noqa: E402  (imported late: it wires back into this module)

api_queue.wire(
    store_getter=get_store,
    reconcile_and_record=_reconcile_and_record,
    parse_bytes=parse_bytes,
    safe_name=_safe_name,
    result_exclude=_RESULT_EXCLUDE,
)
app.include_router(api_queue.router)


if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
else:  # pragma: no cover
    @app.get("/")
    def _no_web() -> dict:
        return {"detail": "web/ not built; API only"}
