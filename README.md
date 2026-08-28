# AI Finance Controller

> **Razorpay AI Buildathon — Track 4: AI Finance Controller.**
> An agent that closes the finance‑ops reconciliation loop across batch data,
> with a measured accuracy, a replayable audit trail, and an honest exception queue.

**[razorpay.com/buildathon](https://razorpay.com/buildathon/)** · "Build. Show. Get hired."

---

## The problem

Every month‑end close, a finance team reconciles four exports that never line up:

| Source | What it says |
| --- | --- |
| **Payments** (gateway) | gross captures, refunds |
| **Settlements** (payout) | net paid to the bank = gross − fee − 18% GST, on a T+2 cycle |
| **Bank statement** | what actually hit the current account |
| **Ledger / books** | what accounting *thinks* happened |

The loop is only "closed" when every rupee is matched across all four, every
unmatched item has a documented reason, and re‑running the process gives the
same answer. Doing this by hand is days of work and the source of most revenue
leakage — payouts that never landed, credits that were never booked.

## What this does

Drop in the four files. The controller:

1. **Normalizes** every row to one schema (integer paise, IST dates, cleaned refs).
2. **Matches deterministically** — a union‑find driven by accounting identities:
   shared references, then `Σ payment gross == settlement net + fee + GST` on the
   T+2 date, then `bank credit == Σ settlement net`. Split batches (many payments →
   one payout) and merged payouts (one credit → many settlements) are resolved by
   a **unique** same‑day subset‑sum. Ambiguity is never guessed.
3. **Hands the residual tail to a resolver** — an LLM with a strict JSON contract
   (temperature 0), or a deterministic heuristic scorer when no key is set. Either
   way the result is conservation‑checked: the resolver can't invent or drop a row.
4. **Explains every exception** — each unmatched row gets a category, a confidence,
   a plain‑English reason, and a suggested action. Nothing is silently dropped.
5. **Writes an audit trail** — one record per decision, with the inputs, the rule
   and version, and the rationale. Re‑running reproduces the result (a fingerprint
   is checked on every run).
6. **Measures itself** — pair‑based precision / recall / F1 against a labelled key,
   exception‑category accuracy, and a rupee summary: reconciled, in‑transit,
   **recoverable**, unrecorded.

## Results

Three bundled benchmark datasets, generated deterministically with a ground‑truth
answer key (`data/datasets/`), scored with the **offline heuristic resolver** (no
LLM key — worst case):

| dataset | entries | auto‑match | precision | recall | F1 | exception‑category accuracy | replay | engine time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| clean | 125 | 100.0% | 1.000 | 1.000 | 1.000 | — (no anomalies) | stable | ~18 ms |
| realistic | 348 | 98.3% | **1.000** | **1.000** | **1.000** | **100%** | stable | ~130 ms |
| messy | 460 | 97.2% | **1.000** | **1.000** | **1.000** | **100%** | stable | ~180 ms |

The un‑matched rows aren't misses — they're the injected anomalies (double‑booked
entries, unrecorded bank credits, bank charges), each correctly categorised.
Regenerate the table any time with `python scripts/run_reconciliation.py`.
Full method: [`docs/METRICS.md`](docs/METRICS.md).

## Run it

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements.txt && pip install -e .

# 1) the dashboard  ->  http://localhost:8000
python -m uvicorn finance_controller.api:app --port 8000

# 2) the CLI
python scripts/run_reconciliation.py --dataset messy --out out/
python scripts/run_reconciliation.py --input your/export/dir --out out/ --labels labels.json
```

Docker:

```bash
docker build -t finance-controller . && docker run -p 8000:8000 finance-controller
```

### Optional integrations

Copy `.env.example` to `.env`:

- `ANTHROPIC_API_KEY` — switches the residual resolver from the heuristic to the LLM.
  Everything still works without it.
- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` (**test mode**) — lets `ingest.load_from_razorpay()`
  pull live payments + settlements instead of files.

## Architecture

`src/finance_controller/` — `ingest` → `normalize` → `reconcile` (deterministic +
structural) → `resolver` (LLM / heuristic) → `exceptions` → `metrics` →
`pipeline` (one `ReconResult`). The FastAPI app in `api.py` serves both the JSON
API and the dashboard in `web/`. Full write‑up: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Tests

```bash
python -m pytest -q        # 44 tests: engine correctness, conservation, synth
python -m ruff check .     # lint
```

## Submission checklist

- [x] Public repository
- [x] Architecture documentation → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [x] Working product with measurable results → the table above, reproducible
- [x] Audit trail — `out/audit.jsonl`, replay‑checked
- [x] Honest exception reporting — categorised, never dropped, conservation‑asserted
- [ ] 5‑minute pitch video → link in [`docs/PITCH.md`](docs/PITCH.md)

## License

MIT — see [`LICENSE`](LICENSE).
