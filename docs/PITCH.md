# Pitch — AI Finance Controller (5 min)

**Video link:** _add an unlisted YouTube / Loom URL here before submitting_

## Beat sheet

| Time | Beat | On screen |
| --- | --- | --- |
| 0:00–0:30 | The month‑end close: four exports, none tie out, days of manual work, and that's where revenue leaks | the three dataset cards |
| 0:30–1:15 | Design: deterministic‑first, LLM‑last, never guess — because reconciliation is mostly exact arithmetic | `docs/ARCHITECTURE.md` pipeline diagram |
| 1:15–2:30 | Live run on **Realistic month** → the dashboard | the app (follow `DEMO.md`) |
| 2:30–3:15 | Accuracy is measured on data the rules have never seen: precision 1.0000, recall 0.9928, F1 0.9962 across five held-out seeds, replay-stable | the KPI row + Accuracy tab |
| 3:15–4:00 | It finds money: ₹9,053 recoverable, ₹1,981 unrecorded — and every exception has a reason and an action | the money bar + Exceptions tab |
| 4:00–4:40 | Every decision is auditable and reproducible | the Audit trail tab; `out/audit.jsonl` |
| 4:40–5:00 | Where the AI is (the residual resolver), what's next (real Razorpay recon report, FX, learned fee rates) | — |

## Numbers to say (all reproducible)

Lead with the **held-out** numbers. They are lower than the dev-seed numbers,
and that is the point — anyone can score 100% on the data they tuned on.

- **Held out, 15 runs over 4,631 entries on five unseen seeds: precision
  1.0000, recall 0.9928, F1 0.9962.** Not one rupee in a wrong group.
- **Generalisation gap: 0.38 F1 points** against the dev seed. That gap is the
  only honest measure of whether the rules generalise or were just fitted.
- Recall is deliberately **not** 1.0 — on one run two same-day payment triples
  sum to the identical rupee, so the engine refuses rather than guessing, and
  takes the hit. Worst single run: 0.8927.
- 466 rows (messy) reconcile in well under a second; 58,908 in about four.
  Quote what is on your screen — throughput moves with the machine.
- Replay-stable: same fingerprint on re-run, 15/15.
- Runs fully offline; the LLM is optional and only touches the ambiguous tail.

Say "100%" only about precision, and only while naming the dataset. If you want
the dev-seed 1.0000 across the board, say "on the seed I tuned on" in the same
breath.

## Claims to keep honest

- State that the scores use the *heuristic* resolver (the floor).
- Show at least one exception and walk its reasoning.
- Say the benchmark is synthetic and that FX settlements aren't modelled yet.
- No "100%" without pointing at the dataset and the one command that reproduces it.
