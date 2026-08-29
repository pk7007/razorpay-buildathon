## What changed

<!-- One or two sentences. What does this do that the code did not do before? -->

## Why

<!-- The problem, not the patch. -->

## Effect on the numbers

The README quotes held-out accuracy and throughput. If this touches
`reconcile.py`, `resolver.py`, `exceptions.py` or `synth.py`, paste the before
and after:

```
python scripts/run_reconciliation.py --evaluate
python scripts/run_reconciliation.py --benchmark
```

| | precision | recall | F1 | ₹ in wrong groups |
| --- | --- | --- | --- | --- |
| before | | | | |
| after | | | | |

## Checklist

- [ ] `python -m pytest -q` passes
- [ ] `python -m ruff check .` passes
- [ ] Held-out precision did not regress (a wrong match costs more than an exception)
- [ ] Datasets still regenerate byte-identically (`python scripts/make_datasets.py`)
- [ ] README / `docs/METRICS.md` updated if any published number moved
- [ ] No secrets, no `.env`, no generated output committed
