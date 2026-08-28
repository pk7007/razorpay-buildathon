# Pitch — AI Finance Controller (5 min)

**Video link:** _add unlisted YouTube / Loom URL here before submitting_

## Beat sheet

| Time | Beat | Show on screen |
| --- | --- | --- |
| 0:00–0:30 | The close problem: 4 sources, none line up, days of manual work | the 4 sample files side by side |
| 0:30–1:15 | Our loop: deterministic-first, LLM-last | `docs/ARCHITECTURE.md` diagram |
| 1:15–2:30 | Live run on sample data | terminal → `out/reconciliation.json`, `out/exceptions.csv` |
| 2:30–3:30 | Every exception explained + audit trail replay | `out/audit.jsonl`, one exception walked through |
| 3:30–4:30 | Measurable results | `out/metrics.json` vs targets in `docs/METRICS.md` |
| 4:30–5:00 | Why it generalizes + what a 6-month internship ships | roadmap bullets |

## Claims to keep honest

- State the exact input files and label set used for every number.
- Show at least one case the agent got wrong or refused, and how the exception
  report surfaced it.
- No "100%" without the labelled set and replay evidence to back it.
