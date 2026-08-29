---
name: Bug report
about: Something reconciles wrongly, crashes, or reports a number you cannot reproduce
title: ''
labels: bug
assignees: ''
---

## What happened

## What you expected

## Reproduce it

```bash
# the exact command, e.g.
python scripts/run_reconciliation.py --dataset messy --out out/
```

If it involves your own exports, the smallest set of rows that shows the problem
is far more useful than the whole file. **Redact real amounts, UTRs and
counterparty names** — this repo must never receive real financial data.

## Which entries

Entry ids from `out/exceptions.csv` or `out/audit.jsonl`, plus the audit record
for the decision you disagree with. Every match and every exception has one.

## Environment

- Python version:
- Resolver (`GET /api/health` → `resolver`): heuristic / llm
- Commit:
