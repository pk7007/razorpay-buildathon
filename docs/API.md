# API

Base URL when running locally: `http://localhost:8000`.
Interactive OpenAPI docs are served at `/docs`, the raw schema at `/openapi.json`.

There is **no authentication**, because there is nothing to authenticate against:
the service stores nothing, owns no account and returns only what you sent it,
reconciled. See [`ARCHITECTURE.md`](ARCHITECTURE.md#no-database-no-auth) for why
that is a decision rather than an omission.

## Conventions

| | |
| --- | --- |
| Money | integer **paise** in every field ending `_paise` (₹1.00 = `100`) |
| Dates | `YYYY-MM-DD`, IST calendar dates |
| Errors | `{"detail": "...", "request_id": "..."}` — never a stack trace |
| Tracing | every response carries `X-Request-ID` and `X-Response-Time-ms` |
| Rate limit | 30 POSTs / minute / client IP → `429` with `Retry-After: 60` |
| Compression | gzip above 1 KB |

---

## `GET /api/health`

Liveness plus which resolver backend is active.

```json
{
  "status": "ok",
  "version": "1.0.0",
  "resolver": "heuristic",
  "razorpay_configured": false,
  "datasets": ["clean", "messy", "realistic"]
}
```

`resolver` is `"llm"` when `ANTHROPIC_API_KEY` is set, otherwise `"heuristic"`.
It never reports anything derived from a key, only whether one exists.

---

## `GET /api/datasets`

The bundled benchmark datasets, ordered by difficulty.

```json
[{ "name": "clean", "label": "Clean", "blurb": "…", "days": 6 }]
```

---

## `POST /api/reconcile`

Reconcile a bundled dataset.

```bash
curl -X POST http://localhost:8000/api/reconcile \
  -H 'content-type: application/json' \
  -d '{"dataset":"realistic"}'
```

| Code | When |
| --- | --- |
| `200` | reconciled — returns a [`ReconResult`](#reconresult) |
| `404` | unknown dataset (also how path traversal is refused) |
| `429` | rate limited |

---

## `POST /api/reconcile/upload`

Reconcile your own exports. `multipart/form-data`, all four parts optional but
**at least one required**. CSV or JSON.

```bash
curl -X POST http://localhost:8000/api/reconcile/upload \
  -F payments=@payments.csv \
  -F settlements=@settlements.csv \
  -F bank=@bank.csv \
  -F ledger=@ledger.csv
```

Column names are matched leniently — see [`normalize.py`](../src/finance_controller/normalize.py).
Payments accept `id, amount, created_at, order_id`; settlements add `fees, tax,
settled_at, utr`; bank accepts `value_date, narration, utr, type`; ledger accepts
`date, memo, external_ref`. Unparseable amounts and dates are tolerated rather
than fatal — they surface as exceptions instead of a 500.

| Code | When |
| --- | --- |
| `200` | reconciled |
| `400` | no files supplied |
| `413` | a file over 8 MB, or more than 100,000 rows total |
| `422` | a file could not be parsed, or contained no rows |
| `429` | rate limited |

Nothing is written to disk. The response is the only copy of the result.

---

## `GET /api/evaluation`

Dev vs held-out accuracy — the numbers the README quotes, served from the
running code so a reader can check the claim rather than trust it.

```json
{
  "dev":     { "runs": 3,  "precision_mean": 1.0, "recall_mean": 1.0, "...": "…" },
  "holdout": { "runs": 15, "precision_mean": 1.0, "recall_mean": 0.9928, "...": "…" },
  "generalisation_gap": { "precision": 0.0, "recall": 0.0072, "f1": 0.0038 },
  "dev_runs": [], "holdout_runs": []
}
```

Cached after the first call (it is a pure function of committed data).

## `GET /api/benchmark`

Throughput at increasing batch sizes.

```json
{
  "runs": [{ "records": 23565, "seconds": 2.22, "records_per_sec": 10622, "...": "…" }],
  "peak_records_per_sec": 13527
}
```

First call takes a few seconds because it actually reconciles ~90,000 records.
Cached thereafter.

---

## ReconResult

The single object every reconcile endpoint returns.

| Field | Meaning |
| --- | --- |
| `dataset` | which batch was run |
| `resolver_mode` | `heuristic` or `llm` — what actually resolved the residual tail |
| `entries[]` | every normalized input row (raw source columns stripped) |
| `groups[]` | matched groups |
| `exceptions[]` | everything the engine could **not** resolve |
| `audit[]` | one record per decision, in order |
| `money` | the rupee summary |
| `metrics` | accuracy, throughput, LLM cost |

**Conservation invariant:** every entry appears in exactly one group **or**
exactly one exception. Asserted server-side on every run; the resolver cannot
invent or lose a row.

### `groups[]`

```json
{
  "group_id": "G0002",
  "entry_ids": ["bank_0360", "ldgr_0350", "pay_0349", "setl_0359"],
  "stage": "structural",
  "rule": "exact-reference@v2, split-settlement@v1",
  "confidence": 0.92,
  "rationale": "5 payment unit(s) sum to ₹6,366.87 gross = settlement net ₹6,216.60 + fee ₹127.34 + GST ₹22.93, T+2",
  "amount_paise": 636687,
  "status": "complete",
  "sources": ["bank", "ledger", "payment", "settlement"]
}
```

`stage` is `deterministic` | `structural` | `heuristic` | `agent` — the last two
mean the residual resolver placed it.

`status`:

| Status | Meaning |
| --- | --- |
| `complete` | all four legs tied |
| `awaiting_settlement` | recent sale, payout not raised yet |
| `awaiting_payout` | settled, bank credit due within the cycle |
| `payout_overdue` | should have landed → **recoverable** |
| `unbooked_payout` | money in the bank, never booked |
| `ambiguous_split` | paid out, but not uniquely attributable to one batch |
| `partial` | legs missing with no clean explanation |

### `exceptions[]`

```json
{
  "entry_id": "bank_0426",
  "source": "bank",
  "amount_paise": 233359,
  "value_date": "2026-07-05",
  "category": "fee_mismatch",
  "confidence": 0.8,
  "rationale": "settlement setl_0425 shares UTR UTR1328518486 but credit is ₹12.88 short — likely a bank / processing charge",
  "suggested_action": "Reconcile the deduction against the settlement / bank charge schedule."
}
```

Categories: `missing_in_bank`, `missing_in_ledger`, `payout_in_transit`,
`fee_mismatch`, `split_settlement`, `merged_payout`, `duplicate`,
`fx_or_adjustment`, `amount_mismatch`, `unknown`.

Every exception carries a **reason** and an **action**. None is a bare
"unmatched".

### `money`

| Field | Meaning |
| --- | --- |
| `gross_processed_paise` | total captured payment volume |
| `reconciled_paise` | gross of fully-tied groups |
| `in_transit_paise` | settled or awaiting payout within the normal cycle |
| `recoverable_paise` | **booked revenue that never reached the bank — chase this** |
| `unrecorded_paise` | bank credits with no ledger entry |
| `ambiguous_paise` | paid out, not uniquely attributable |
| `in_exception_paise` | total value sitting in exceptions |

### `metrics`

`precision`, `recall`, `f1` and `exception_category_accuracy` are `null` unless
the run had a ground-truth key (bundled datasets do; uploads do not).
`replay_stable` is the result of re-running and comparing a fingerprint.
`llm_calls`, `llm_input_tokens`, `llm_output_tokens` and `llm_cost_usd` are
populated only when the LLM resolver actually ran.

### `audit[]`

```json
{
  "seq": 1,
  "ts": "2026-08-29T08:47:57Z",
  "stage": "structural",
  "rule": "payment-to-settlement@v1",
  "inputs": ["pay_0161", "setl_0162"],
  "outcome": "matched",
  "confidence": 0.98,
  "rationale": "1 payment unit(s) sum to ₹595.61 gross = settlement net ₹581.56 + fee ₹11.91 + GST ₹2.14, T+2"
}
```

`outcome` is `matched` | `exception` | `rejected`. Rejected records exist so that
a refused LLM proposal or an undecidable split is visible rather than silent.
Rules are versioned (`@v1`), so a decision can always be traced to the exact
logic that produced it.
