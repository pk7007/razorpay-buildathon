# AI Finance Controller — Razorpay AI Buildathon

> Track 4: **AI Finance Controller** — an agent that closes finance-ops loops with
> measured reconciliation accuracy across batch data, a full audit trail, and
> honest exception reporting.

**Buildathon:** [razorpay.com/buildathon](https://razorpay.com/buildathon/) · "Build. Show. Get hired."

---

## Problem

Finance teams spend most of a close cycle reconciling four sources that never
line up perfectly:

| Source | Example rows |
| --- | --- |
| **Payments** (gateway) | captured payments, refunds, disputes |
| **Settlements** (payout to bank) | net settlement batches, fees, tax, adjustments |
| **Bank statement** | actual credits/debits that hit the account |
| **Ledger / books** | what accounting *thinks* happened |

The loop is only "closed" when every rupee is matched across all four, every
unmatched item has a documented reason, and the result is reproducible.

## What this builds

An agent that:

1. **Ingests** batch exports from each source (CSV/JSON, Razorpay test-mode API).
2. **Reconciles** deterministically first (exact + tolerant matching on
   amount / date / reference), then uses an LLM only for the residual
   ambiguous set (fuzzy descriptions, split/merged payouts, timing gaps).
3. **Explains every exception** — no silent drops. Each unmatched row gets a
   category, a confidence, and a suggested action.
4. **Writes an audit trail** — every match and every agent decision is logged
   with inputs, rule/version, and rationale so a human can replay it.
5. **Measures itself** — precision / recall / auto-match rate against a labelled
   set, reported honestly in `docs/METRICS.md`.

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Short version:

```
ingest ─► normalize ─► deterministic match ─► LLM match (residual only)
                                   │                    │
                                   ▼                    ▼
                              audit log ◄─────── exception report ──► METRICS
```

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env           # add RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (test mode) + LLM key
python scripts/run_reconciliation.py --input data/sample --out out/
```

Output:

- `out/reconciliation.json` — matched groups + unmatched items
- `out/exceptions.csv` — every unmatched row with category, confidence, action
- `out/audit.jsonl` — append-only decision log
- `out/metrics.json` — precision / recall / auto-match rate (when labels present)

## Submission checklist

- [x] Public repository
- [ ] 5-minute pitch video → link in [`docs/PITCH.md`](docs/PITCH.md)
- [x] Architecture documentation → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [ ] Working product with **measurable results** → [`docs/METRICS.md`](docs/METRICS.md)
- [x] Audit trail
- [x] Honest exception reporting

## Tech

Python 3.11 · Razorpay test-mode APIs · pytest · an LLM for residual matching only.

## License

MIT — see [`LICENSE`](LICENSE).
