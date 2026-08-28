# Architecture — AI Finance Controller

## Goal

Close the finance-ops reconciliation loop across four batch sources with a
**deterministic-first, LLM-last** pipeline, producing a reproducible result, a
replayable audit trail, and honest metrics.

## Data sources

| Source | Key fields | Normalized to |
| --- | --- | --- |
| Payments (Razorpay `payments` / `refunds`) | `id`, `amount` (paise), `created_at`, `order_id`, `status` | `Entry(source=payment)` |
| Settlements (`settlements` + `settlement.recon`) | `id`, `amount`, `fees`, `tax`, `settled_at`, `utr` | `Entry(source=settlement)` |
| Bank statement | `value_date`, `amount`, `narration`, `utr/ref` | `Entry(source=bank)` |
| Ledger / books | `date`, `amount`, `memo`, `external_ref` | `Entry(source=ledger)` |

All amounts are integer **paise**. All timestamps are converted to IST dates.

## Pipeline

```
                    ┌─────────────┐
  raw exports  ───► │  ingest.py  │  parse CSV/JSON, Razorpay API pull
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │ normalize   │  → List[Entry]  (canonical schema, paise, IST)
                    └──────┬──────┘
                           ▼
                    ┌──────────────────────┐
                    │ deterministic match  │  Stage 1
                    │  1. exact key match  │  (utr / ref / order_id)
                    │  2. tolerant match   │  amount ±tol, date ±tol
                    └──────┬───────────────┘
                     matched│      │ residual (ambiguous / unmatched)
                            ▼      ▼
                    audit.log   ┌──────────────────────┐
                                │ LLM match (agent)    │  Stage 2
                                │  proposes groupings  │  residual set ONLY
                                │  + rationale + conf  │
                                └──────┬───────────────┘
                          accepted ≥ θ │      │ < θ or refused
                                       ▼      ▼
                                 audit.log   exceptions.py
                                                  │
                                                  ▼
                                          exceptions.csv  (category, confidence, action)
                                                  │
                              ┌───────────────────┴──────────────┐
                              ▼                                  ▼
                        reconciliation.json                 metrics.py → metrics.json
```

## Modules (`src/finance_controller/`)

| Module | Responsibility |
| --- | --- |
| `config.py` | env + tolerances, single `Settings` object |
| `models.py` | `Entry`, `MatchGroup`, `Exception`, `AuditRecord` (pydantic) |
| `ingest.py` | file + Razorpay test-mode API loaders → raw dicts |
| `normalize.py` | raw dict → canonical `Entry` |
| `reconcile.py` | Stage 1 deterministic matcher |
| `agent.py` | Stage 2 LLM agent over the residual set |
| `exceptions.py` | categorize + score every unmatched entry |
| `audit.py` | append-only JSONL writer, one record per decision |
| `metrics.py` | precision / recall / auto-match rate vs labels |
| `pipeline.py` | wires the stages, writes `out/` |

## Matching rules (Stage 1)

1. **Exact** — same `utr` OR same `order_id` OR same `external_ref`, and amount
   equal after fee/tax adjustment.
2. **Tolerant** — amount within `AMOUNT_TOLERANCE_PAISE`, date within
   `DATE_TOLERANCE_DAYS`, and exactly one candidate on each side. Ambiguity
   (>1 candidate) is pushed to the residual set, never guessed.

Rule id + version is stamped on every `AuditRecord` so a match can be traced to
the exact logic that produced it.

## Agent (Stage 2)

- Input: only the residual entries (typically <10% of volume).
- The model proposes match groups with a `rationale` and `confidence`.
- Groups with `confidence >= θ` (default 0.80) and passing a hard amount-sum
  check are accepted; everything else becomes an exception.
- The agent may **never** invent an entry or silently drop one — the post-check
  asserts `set(input_ids) == set(matched_ids) ∪ set(exception_ids)`.

## Exception categories

`timing_gap`, `fee_mismatch`, `split_settlement`, `merged_payout`,
`missing_in_bank`, `missing_in_ledger`, `duplicate`, `fx_or_adjustment`,
`unknown`. Each row: `{entry_id, category, confidence, suggested_action}`.

## Audit trail

`out/audit.jsonl`, append-only. One record per decision:

```json
{
  "ts": "2026-08-28T10:15:03+05:30",
  "stage": "deterministic|agent",
  "rule": "tolerant-match@v1",
  "inputs": ["pay_123", "bank_998"],
  "outcome": "matched|exception",
  "confidence": 1.0,
  "rationale": "amount equal (paise), value_date within 1 day, single candidate"
}
```

Replaying the log against the same inputs must reproduce `reconciliation.json`
byte-for-byte (deterministic stage) or group-for-group (agent stage, temp=0).

## Reproducibility

- LLM calls pinned to `temperature=0` and a fixed model id.
- All randomness seeded.
- `out/` is regenerated, never edited by hand.
