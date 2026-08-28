"""FastAPI app: reconciliation as a service + the dashboard.

    uvicorn finance_controller.api:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from .config import SETTINGS
from .ingest import available_datasets, parse_bytes
from .models import ReconResult
from .pipeline import run_bundled, run_rows
from .synth import PROFILES

_WEB = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # pay the import / model-build cost once so the first real request is fast
    try:
        run_rows({"payment": [], "settlement": [], "bank": [], "ledger": []}, dataset="warmup")
        for name in available_datasets():
            run_bundled(name)
            break
    except Exception:  # noqa: BLE001 - warmup is best-effort
        pass
    yield


app = FastAPI(
    title="AI Finance Controller",
    version="1.0.0",
    description="Deterministic-first reconciliation across payments, settlements, bank and ledger.",
    lifespan=_lifespan,
)

_DATASET_BLURB = {
    "clean": "Well-behaved month — fees, GST, a T+2 payout cycle, one batch, "
    "one payout in transit.",
    "realistic": "A normal month: split batches, merged payouts, a double-booked entry, "
    "an unrecorded credit, a bank charge, and revenue that never settled.",
    "messy": "A rough month — every anomaly, more often. The stress test.",
}


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "resolver": "llm" if SETTINGS.has_llm else "heuristic",
        "razorpay_configured": SETTINGS.has_razorpay,
    }


_ORDER = ["clean", "realistic", "messy"]


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
_MAX_FILE_BYTES = 8_000_000
_MAX_ROWS = 20_000


@app.post(
    "/api/reconcile",
    response_model=ReconResult,
    response_model_exclude_none=True,
    response_model_exclude=_RESULT_EXCLUDE,
)
def reconcile_dataset(payload: dict) -> ReconResult:
    name = (payload or {}).get("dataset")
    if name not in available_datasets():
        raise HTTPException(404, f"unknown dataset {name!r}")
    return run_bundled(name)


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
) -> ReconResult:
    files = {"payment": payments, "settlement": settlements, "bank": bank, "ledger": ledger}
    if not any(files.values()):
        raise HTTPException(400, "upload at least one file")
    rows: dict = {}
    total = 0
    for source, up in files.items():
        if up is None:
            rows[source] = []
            continue
        blob = await up.read()
        if len(blob) > _MAX_FILE_BYTES:
            raise HTTPException(413, f"{up.filename} exceeds 8 MB")
        try:
            rows[source] = parse_bytes(up.filename or f"{source}.csv", blob)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, f"could not parse {up.filename}: {exc}") from exc
        total += len(rows[source])
    if total > _MAX_ROWS:
        raise HTTPException(413, f"{total} rows exceeds the {_MAX_ROWS}-row demo limit")
    return run_rows(rows, dataset="upload")


if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
else:  # pragma: no cover
    @app.get("/")
    def _no_web() -> dict:
        return {"detail": "web/ not built; API only"}
