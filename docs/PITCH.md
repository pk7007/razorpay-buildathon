# Pitch — AI Finance Controller (5 min)

**Video link:** _add an unlisted YouTube / Loom URL here before submitting_

## Beat sheet

| Time | Beat | On screen |
| --- | --- | --- |
| 0:00–0:30 | The month‑end close: four exports, none tie out, days of manual work, and that's where revenue leaks | the three dataset cards |
| 0:30–1:15 | Design: deterministic‑first, LLM‑last, never guess — because reconciliation is mostly exact arithmetic | `docs/ARCHITECTURE.md` pipeline diagram |
| 1:15–2:30 | Live run on **Realistic month** → the dashboard | the app (follow `DEMO.md`) |
| 2:30–3:15 | Accuracy is measured, not claimed: 100% precision/recall vs a ground‑truth key, replay‑stable | the KPI row + Metrics tab |
| 3:15–4:00 | It finds money: ₹9,053 recoverable, ₹1,981 unrecorded — and every exception has a reason and an action | the money bar + Exceptions tab |
| 4:00–4:40 | Every decision is auditable and reproducible | the Audit trail tab; `out/audit.jsonl` |
| 4:40–5:00 | Where the AI is (the residual resolver), what's next (real Razorpay recon report, FX, learned fee rates) | — |

## Numbers to say (all reproducible)

- 460 rows reconciled in ~180 ms, **100% precision / 100% recall / 100% F1**.
- **100%** exception‑category accuracy (13/13 on the messy dataset).
- Replay‑stable — same fingerprint on re‑run.
- Runs fully offline; LLM is optional and only touches the ambiguous tail.

## Claims to keep honest

- State that the scores use the *heuristic* resolver (the floor).
- Show at least one exception and walk its reasoning.
- Say the benchmark is synthetic and that FX settlements aren't modelled yet.
- No "100%" without pointing at the dataset and the one command that reproduces it.
