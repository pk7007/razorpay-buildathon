# Architecture — AI Finance Controller

## Principle

**Deterministic‑first, LLM‑last, never guess.** Accounting reconciliation is
mostly exact arithmetic; an LLM is only useful for the small ambiguous tail, and
only if its output is checked. Every stage is auditable and the whole run is
reproducible.

## Pipeline

```
 raw exports ─► ingest ─► normalize ─► reconcile ──────────────► resolver ─────► exceptions ─► metrics
 (CSV/JSON /   parse    canonical    deterministic + structural   LLM or        categorise    P/R/F1,
  Razorpay    rows      Entry list   union-find over entry ids    heuristic     the residual  money,
  test API)                          (identities, never guess)    (conservation)  tail        audit, replay
```

One call → one `ReconResult` (`models.py`): the entries, the match groups, the
exceptions, the audit log, the money summary, the metrics.

## Modules (`src/finance_controller/`)

| Module | Responsibility |
| --- | --- |
| `config.py` | env + tolerances in one frozen `Settings` (`has_llm`, `has_razorpay`) |
| `models.py` | `Entry`, `MatchGroup`, `ReconException`, `AuditRecord`, `MoneySummary`, `RunMetrics`, `ReconResult` (pydantic) |
| `synth.py` | deterministic benchmark generator + ground‑truth answer key |
| `ingest.py` | load a directory, a bundled dataset, or the Razorpay test‑mode API; `parse_bytes` for uploads |
| `normalize.py` | raw dict → canonical `Entry`; tolerant of junk (bad amounts/dates never crash a run) |
| `reconcile.py` | the deterministic + structural matcher (below) |
| `resolver.py` | residual resolver — LLM backend or heuristic backend, same contract |
| `exceptions.py` | categorise every unmatched row; pull duplicates out of otherwise‑good groups |
| `metrics.py` | pair‑based scoring, per‑group completeness status, rupee money summary, replay fingerprint |
| `pipeline.py` | wires the stages; asserts conservation; runs the replay check |
| `api.py` | FastAPI: JSON API + serves the `web/` dashboard |

## Canonical model

Money is **integer paise** everywhere. Dates are IST calendar dates. `Entry`
carries `source`, `amount_paise`, `value_date`, a cleaned `reference`
(UTR / order id / external ref, also mined out of bank narrations), and for
settlements `fee_paise` + `tax_paise`.

## Matching (`reconcile.py`)

A **union‑find over entry ids**. Each rule contributes merge edges tagged with a
rule name, a confidence and a rationale; connected components become match
groups. Rules run strongest‑first:

1. **`exact-reference`** — entries sharing a cleaned reference *and* agreeing on
   amount (a shared ref with incompatible amounts — e.g. a bank charge — is *not*
   force‑joined). Confidence 1.0.
2. **`payment-to-settlement`** — the identity `Σ payment gross == net + fee + GST`
   on the T+2 date. 1:1 links resolve first (unambiguous), which clears noise so
   split batches (`split-settlement`) can be matched by a **unique same‑day
   subset‑sum**. Confidence 0.92–0.98.
3. **`bank-to-settlement` / `merged-bank-credit`** — `bank credit == Σ settlement
   net` on the value date; 1:1 first, then a unique subset for consolidated
   payouts. Confidence 0.88–0.96.

Subset‑sum is bounded: k=2/k=3 use a two‑pointer scan, the search is partitioned
by calendar day, and any ambiguity (more than one viable subset) returns nothing
and defers to the resolver. Engine time stays sub‑200 ms for ~500 entries.

Each group gets a **completeness status** (`complete`, `awaiting_payout`,
`payout_overdue`, `unbooked_payout`, …) from which legs are present and how old
it is — this is what drives the "recoverable" rupee figure.

## Residual resolver (`resolver.py`)

Input = the entries no deterministic rule placed. Two interchangeable backends:

- **`llm`** — one call, `temperature=0`, strict JSON contract
  (`{"groups":[{"entry_ids,confidence,rationale}]}`). Proposals below the
  confidence threshold or naming unknown ids are rejected and audited. Token
  counts and USD cost are recorded.
- **`heuristic`** — a deterministic pair scorer over amount closeness, date
  proximity, narration‑token Jaccard and reference echo. Used automatically when
  no `ANTHROPIC_API_KEY` is set, so the product is 100% functional offline.

The pipeline then **asserts conservation**: every entry is in exactly one group
or is exactly one exception — the resolver cannot invent or lose a row.

## Exceptions (`exceptions.py`)

Everything still unmatched becomes an explained exception:
`missing_in_bank` (booked, never settled → recoverable), `missing_in_ledger`
(bank credit never booked), `fee_mismatch` (short by a bank charge, with the
matching settlement named), `duplicate` (same amount booked twice), … Each row:
`{entry_id, category, confidence, rationale, suggested_action}`.

## Audit & reproducibility

`ReconResult.audit` is an ordered list of `AuditRecord` — one per decision, with
inputs, rule@version, outcome, confidence, rationale. The CLI writes it to
`out/audit.jsonl`. `metrics.result_fingerprint()` hashes the groups + exceptions;
`pipeline.run_rows` re‑runs once and sets `replay_stable` from whether the
fingerprints match. LLM calls are pinned to `temperature=0`.

## Metrics (`metrics.py`)

- **Precision / recall / F1** on *pairs* (two entries in the same group), which
  avoids rewarding trivially large groups.
- **Exception‑category accuracy** vs the dataset's `truth.json`.
- **Money summary** in rupees: `reconciled`, `in_transit`, `recoverable`,
  `unrecorded`, `in_exception`.
- **Latency**, and when the LLM ran: calls, tokens, USD cost.

## Frontend (`web/`)

Buildless: native ES modules, hand-written CSS, no bundler and no
`node_modules`. Six screens behind a hash router.

```
web/
├── index.html              app shell; loads three stylesheets and one module
├── favicon.svg
├── fonts/                  IBM Plex latin subset, self-hosted (7 files, 222 KB)
├── styles/
│   ├── fonts.css           @font-face declarations
│   ├── tokens.css          colour, type, spacing, elevation, motion — all three
│   │                       theme states (system / explicit light / explicit dark)
│   ├── base.css            reset, typography, app shell, responsive rail
│   └── components.css      badges, metrics, grids, drawer, dropzones, toasts
└── js/
    ├── theme.js            pre-paint theme application (separate file: CSP is
    │                       `script-src 'self'`, so no inline script)
    ├── app.js              rail, topbar, routing, the rail's unresolved count
    ├── router.js           hash routing with a stale-render guard
    ├── api.js              the only module that calls the server
    ├── format.js           money, dates, percentages — Indian digit grouping
    ├── ui.js               DOM builder, icons, badges, states, drawer, toasts
    └── views/
        ├── dashboard.js        close overview
        ├── reconcile.js        pick a batch, read the result (also renders the
        │                       result for an upload)
        ├── exceptions.js       the worklist
        ├── exception-detail.js the investigation drawer
        └── misc.js             run history, accuracy evidence, file import
```

Two rules the frontend keeps:

- **Nothing renders through `innerHTML` except icon SVG.** Bank narrations are
  attacker-controllable text; every node is built with `textContent`, so a
  narration can never become markup.
- **Colour is never the only channel.** Every badge carries a dot as well as a
  hue, and the brand accent is indigo precisely because green / amber / red are
  already reserved for financial state.

## Deploy

`Dockerfile` (python:3.11‑slim, `uvicorn` on `:8000`, healthcheck on
`/api/health`), `render.yaml` blueprint, `Procfile`. No database — a run is
in‑memory and the response is the only copy of the result.
