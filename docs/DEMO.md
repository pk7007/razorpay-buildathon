# Live demo — 90 seconds

**Setup:** `python -m uvicorn finance_controller.api:app --port 8000` → open
`http://localhost:8000`. First request is pre‑warmed on startup.

## The path

| # | Action | What the judge sees | Say |
| --- | --- | --- | --- |
| 1 | Land on the page | "Close a month's books in one click." + three dataset cards | "Finance teams burn days every month reconciling four exports that never tie out." |
| 2 | Click **Realistic month** | ~300 ms later: the results dashboard | "One click. 348 rows across payments, settlements, bank and ledger — reconciled." |
| 3 | Point at the **KPI row** | 98.3% auto‑match · 100% precision · 100% recall · 100% exception accuracy · ~130 ms · replay stable ✓ | "Measured against a ground‑truth key. Deterministic — re‑run and the fingerprint matches." |
| 4 | Point at the **money bar** | reconciled ₹2.0L · **recoverable ₹9,053** (red) · unrecorded ₹1,981 | "It doesn't just tick boxes — it finds the ₹9,053 of revenue that was booked but never settled to the bank." |
| 5 | Open **Matched groups**, expand one split‑batch row | 5 payments + 5 ledger lines + 1 settlement + 1 bank credit, with the rationale: *"5 payment units sum to ₹6,366.87 gross = net ₹6,216.60 + fee ₹127.34 + GST ₹22.93, T+2"* | "Every match is an accounting identity, not a guess — and it shows its working." |
| 6 | Open **Exceptions (6)** | six cards, each a category + a plain reason + a **Do:** action | "Nothing is silently dropped. This bank line is ₹12.88 short of its settlement — a bank charge — and here's what to do about it." |
| 7 | Open **Audit trail** | one row per decision, stage‑tagged, with the reasoning | "Full audit trail. A human can replay any decision." |
| 8 | (optional) **Run another → Messy month** | 460 rows, still 100% / 100%, ~180 ms | "Scales to the worst month with every anomalated more often — same scores." |

## The one‑liner

> "Deterministic‑first reconciliation that closes the four‑way loop in under a
> second, proves its own accuracy, explains every exception, and tells you
> exactly how much money to go chase."

## If asked "where's the AI?"

The AI is the **residual resolver** — the ambiguous tail that exact rules can't
place. It runs an LLM (temperature 0, strict JSON, conservation‑checked) when a
key is set, and a deterministic heuristic otherwise, so the demo never depends on
a network call. The scores shown use the *offline* backend — the floor, not the
ceiling. The design choice being demonstrated is that a finance agent should be
*mostly* deterministic and only reach for a model where it genuinely helps.

## Don't

- Don't upload a random CSV live — the bundled datasets are the reliable path.
- Don't promise FX handling; it's called out as future work.
