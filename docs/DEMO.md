# Live demo — 5 minutes

**Setup**, in this order — the reset cannot run while the server holds the
database open:

```bash
python scripts/demo_reset.py                                   # must print 23 PASS, 0 FAIL
python -m uvicorn finance_controller.api:app --port 8000
```

Then open `http://localhost:8000`. `demo_reset` leaves the worklist with one
item **investigating** and one **resolved**, so the queue looks like a day in
progress rather than a wall of untouched rows. Do not empty the database
instead: the reset is what checks the state against an answer key, and "it ran
without an error" is not the same as producing the expected 13 groups and 4
exceptions.

**Record with the LLM off.** Comment out `ANTHROPIC_API_KEY` in `.env` before
starting. Every number below was produced that way, and it removes a network
round-trip from the middle of a live demo.

The line to land: **this is not a matcher, it is a reconciliation workflow.**

---

## The path

| # | Do | Shows | Say |
| --- | --- | --- | --- |
| 1 | Land on the page | four exports that never tie out | *"Stripe bought a Bengaluru company called Recko in 2021 just to solve this. This is a merchant's month-end."* |
| 2 | Click **Realistic month** | 352 rows, ~150 ms, KPI row | *"One click. Deterministic rules first — an LLM never decides where money went."* |
| 3 | Point at the **KPI row** | 98.3% auto-match · precision/recall on **held-out** seeds · replay stable | *"Measured on five seeds the matcher has never seen. I tuned on one dataset and report on data it has never touched."* |
| 4 | Point at the **money bar** | **recoverable ₹9,053** in red | *"It found ₹9,053 booked as revenue that never reached the bank."* ← **wow moment** |
| 5 | Expand a split-settlement group | 5 payments + ledger + settlement + bank, with the arithmetic | *"Every match is an accounting identity — gross equals net plus fee plus GST plus TDS — not a guess. And it shows its working."* |
| 6 | Open **Evidence** tab | dev vs held-out, side by side | *"The generalisation gap is 0.38 F1 points. That gap is the only honest measure of whether rules generalise or were just fitted."* |

### The half that makes it a product

| # | Do | Shows | Say |
| --- | --- | --- | --- |
| 7 | Click **Worklist** | the queue, with a badge | *"Reconciling once is easy. Real teams work the exceptions over days."* |
| 8 | Open the top item | facts, plain-English reason, **Do:** action, priority | *"Not 'unmatched'. A reason, and what to do about it."* |
| 9 | Mark **investigating**, add a note, assign it | history builds live | *"I called the bank; they're tracing the UTR."* |
| 10 | Go back to **Reconcile** → run the batch **again** | — | *"Now the crucial part. I reconcile the whole month again."* |
| 11 | Return to **Worklist**, open the same item | **status, note and assignee all intact**; `seen 2×` | *"My work survived. The queue updated instead of duplicating — and the item visibly aged."* ← **the real wow moment** |
| 12 | Point at an auto-resolved item (or the `carried forward` KPI) | resolution reason + `auto_resolved` event | *"And when a later run explains an item — the July sale whose August payout finally arrived — it closes itself and records which run did it."* |

### Close on the hard bits

| # | Do | Shows | Say |
| --- | --- | --- | --- |
| 13 | Terminal: `python scripts/run_reconciliation.py --benchmark` | 58,908 records in ~4-5 s | *"Fifty-nine thousand records in about four seconds, single process."* — quote the number on your screen, not one from here; throughput moves with the machine |
| 14 | Point at the **This run** panel: *Resolver — deterministic only* | the whole demo already ran with no model | *"Everything you just saw ran with the LLM switched off. That is the floor, not the ceiling."* — if you kept the key on instead, uncomment it, restart, and re-run to show the resolver flip |
| 15 | **Audit trail** tab | one record per decision | *"An LLM should never decide where your money went. It should only make a suggestion that a rule then checks. That's the whole architecture."* |

---

## If you only have 90 seconds

Steps **2 → 4 → 9 → 10 → 11**. Reconcile, find the recoverable money, work an
item, re-run, show the work survived. That single sequence is the entire
argument.

## Backup answers

**"Is this real Razorpay data?"**
The bundled datasets are synthetic — that is what Track 4 asks for. But the
Razorpay integration is live: with test-mode credentials configured,
`/api/razorpay/status` reports `live_test` and `reachable: true`, and a verified
run pulled 4 real payments from a test-mode account. Be precise about what that
proves — Razorpay test mode issues no settlements, so those payments have no
payout side to match against and reconcile to a 0% auto-match rate with every
row a reasoned exception. It proves the **ingestion path** against
Razorpay-generated data, not a closed four-way loop. Without credentials the
same endpoint serves clearly labelled `fixture` data and says so. It refuses to
run at all against a non-`rzp_test_` key.

**"What about my bank's CSV?"**
Show `POST /api/ingest/preview`. Column detection is verified against HDFC,
ICICI, SBI, Axis and Kotak header layouts, and folds split debit/credit columns
into one signed amount. Where two columns could both be `amount`, it stops and
asks rather than guessing — mapping the wrong column would corrupt a run
invisibly.

**"Where's the AI?"**
The residual resolver — the ambiguous tail exact rules cannot place. It runs an
LLM under a strict JSON contract, and a deterministic heuristic
otherwise. Its proposals are checked arithmetically before they count, and a
conservation assertion makes hallucination structurally impossible. The numbers
on screen were produced with the LLM **off** — that is the floor, not the
ceiling.

## Don't

- Don't upload a random CSV live — use the preview endpoint if asked.
- Don't claim production readiness. Say: *a strong implementation with a
  realistic path to production*, and name the gaps (see the README).
