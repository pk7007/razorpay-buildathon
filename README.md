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

The matching rules were written against **one** seed. Scoring them on that seed
would measure how well they were fitted, not whether they work — so everything
below is scored on **five seeds the rules have never seen**, with the offline
heuristic resolver (no LLM key: the worst case, not the best).

| | runs | entries | precision | worst run | recall | F1 | exception‑category accuracy | ₹ in wrong groups |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev (rules tuned here) | 3 | 943 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 100.0% | ₹0 |
| **held‑out (unseen seeds)** | **15** | **4,631** | **1.0000** | **1.0000** | **0.9928** | **0.9962** | **96.8%** | **₹0** |

**Generalisation gap: 0.38 F1 points.** Every run replay‑stable.

Precision is the metric held hardest, and it is the one reported per‑run rather
than only as a mean: a wrong auto‑match silently closes the book on real money,
while an exception merely asks a human. **Not one rupee landed in an incorrect
group across 4,631 held‑out entries.**

Recall is deliberately *not* 1.0. On one of fifteen runs the engine hits a batch
payout where two different same‑day payment triples sum to the identical rupee.
Amounts alone cannot decide it, so it refuses to guess, files an
`ambiguous_split` with the reason, and takes the recall hit. That is the
intended behaviour.

### Throughput

| records | time | records/sec |
| --- | --- | --- |
| 1,126 | 0.08 s | 13,527 |
| 5,840 | 0.52 s | 11,194 |
| 23,565 | 2.22 s | 10,622 |
| 58,908 | 6.56 s | 8,976 |

Single process, no database. Across a 50× range throughput degrades 1.5× —
effectively linear. Track 4 asks for a 50+ record batch.

Reproduce all of it:

```bash
python scripts/run_reconciliation.py --evaluate     # the held-out table
python scripts/run_reconciliation.py --benchmark    # the throughput table
```

or hit `GET /api/evaluation` and `GET /api/benchmark` on the running service.
Full method and the anomaly catalogue: [`docs/METRICS.md`](docs/METRICS.md).

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

**There is deliberately no database and no auth.** A reconciliation is a pure
function of the four batches it is handed; persisting it would add a breach
surface and a consistency problem while adding nothing to accuracy. The response
is the only copy of a result.

### Where the AI is, and where it is not

The LLM resolves only the **residual tail** — entries no accounting identity can
place, such as an off-gateway bank credit that shares no identifier with the
ledger entry it belongs to. It is not in the critical path:

- **It works with the AI switched off.** No key ⇒ a deterministic heuristic
  resolver takes over. Every number in this README was produced that way.
- **Its output is checked, not trusted.** A proposal must name only real ids and
  must *arithmetically reconcile* before it is accepted — the model's confidence
  is an opinion, the arithmetic is a fact, and the fact wins.
- **It cannot invent or lose a row.** The pipeline asserts that every entry ends
  in exactly one group or exactly one exception.
- **It is bounded**: temperature 0, 20 s timeout, 2 retries, a 150-entry cap, and
  untrusted narration text flattened before it reaches the prompt.

## Tests

```bash
python -m pytest -q            # 93 tests
python -m pytest -q -m slow    # + the throughput benchmark test
python -m ruff check .         # lint
```

Covering: engine correctness, the conservation invariant, held-out
generalisation thresholds, synth determinism, the API contract and every error
path, resolver behaviour under a mocked model (including prompt injection), and
security regressions (path traversal, error leakage, resource limits).

## Track 4 bar, line by line

> *"Build an agent that closes **one finance-ops loop** across a **50+ record
> batch of synthetic data**, reporting its **match rate** and the **exceptions it
> could not resolve**." Bar: "**Throughput** plus **measured accuracy** plus an
> **honest exception list**."*

| Requirement | Where it is |
| --- | --- |
| one finance-ops loop | four-way settlement reconciliation, and only that |
| 50+ record synthetic batch | 125 / 352 / 466 bundled; 58,908 in the benchmark |
| match rate reported | auto-match rate, per run and aggregated |
| exceptions it could not resolve | categorised queue, each with a reason and an action |
| throughput | table above, `--benchmark`, `GET /api/benchmark` |
| measured accuracy | held-out table above, `--evaluate`, `GET /api/evaluation` |
| honest exception list | non-zero, reasoned, and the one run it fails is written up |

## Submission checklist

- [x] Public repository
- [x] Architecture documentation → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [x] Working product with measurable results → held-out table, reproducible
- [x] Audit trail — `out/audit.jsonl`, replay‑checked
- [x] Honest exception reporting — categorised, never dropped, conservation‑asserted
- [ ] 5‑minute pitch video → script ready in [`docs/PITCH.md`](docs/PITCH.md), not yet recorded
- [ ] Deployed URL — `render.yaml` ready, not yet deployed

## License

MIT — see [`LICENSE`](LICENSE).
