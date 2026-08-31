# Development

## Setup

Python **3.11+** is the only hard requirement. Everything else installs from
`requirements.txt`.

```bash
git clone https://github.com/pk7007/razorpay-buildathon.git
cd razorpay-buildathon

python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
pip install -e .                # editable install, so `finance_controller` imports
```

No `.env` is needed. The product is fully functional without any key — the
residual resolver falls back to a deterministic heuristic. Copy `.env.example`
to `.env` only when you want the LLM resolver or the Razorpay test-mode pull.

## Run it

```bash
# dashboard + API
python -m uvicorn finance_controller.api:app --reload --port 8000

# CLI
python scripts/run_reconciliation.py --dataset messy --out out/
python scripts/run_reconciliation.py --input path/to/exports --out out/
python scripts/run_reconciliation.py --evaluate      # held-out accuracy
python scripts/run_reconciliation.py --benchmark     # throughput
```

## Tests

```bash
python -m pytest -q              # 896 tests
python -m pytest -q -m slow      # + the throughput benchmark
python -m ruff check .           # lint
python -m ruff check --fix .
```

What the suites cover:

| File | Covers |
| --- | --- |
| `test_engine.py` | matching correctness, the conservation invariant, replay stability, junk input |
| `test_evaluate.py` | **held-out generalisation thresholds** — the accuracy claim, asserted |
| `test_synth.py` | the benchmark generator is deterministic and self-consistent |
| `test_api.py` | endpoint contracts and every error path |
| `test_resolver.py` | the LLM contract under a mocked model, including prompt injection |
| `test_llm_live.py` | the live model branch: bounds, cost accounting, blast radius, every failure mode |
| `test_razorpay_live.py` | the live Razorpay branch: guards, epoch/paise parsing, failure modes |
| `test_security.py` | path traversal, error leakage, resource limits, headers |

## The rule that matters

**Precision outranks recall.** A wrong auto-match silently closes the book on
real money; an exception costs a human two minutes. When a rule cannot decide
between two possibilities it must decline, record why, and let the entry become
an exception. `tests/test_evaluate.py` enforces this — held-out precision has a
floor and `false_match_cost_paise` must stay at zero.

## Changing the matcher

The datasets in `data/datasets/` are the exam. `reconcile.py` is the answer.
Both were written by the same person, which is exactly why the evaluation is
split:

- **seed 7** (`clean` / `realistic` / `messy`) is the **dev** set — the rules were
  tuned against it. Improvements here prove nothing on their own.
- **seeds 101–105** are **held out**. Never open them while writing a rule.

So the loop is:

```bash
# 1. change a rule
# 2. check you did not break generalisation
python scripts/run_reconciliation.py --evaluate
# 3. check you did not break throughput
python scripts/run_reconciliation.py --benchmark
# 4. full suite
python -m pytest -q
```

If held-out precision drops, the change is wrong even if dev improves.

### Regenerating datasets

```bash
python scripts/make_datasets.py
git diff --stat data/       # must be empty — the generator is deterministic
```

CI fails if regenerating produces a diff, because a benchmark that drifts makes
every published number meaningless. To add a new scenario, extend `synth.py`
**and** its ground-truth label so the scenario is measurable, not just present.

## Performance

Three O(n²)s have been removed here already, all found by profiling rather than
guessing. If throughput regresses:

```bash
python -c "
import cProfile, pstats
from finance_controller.evaluate import _synth_at_scale
from finance_controller.pipeline import run_rows
rows, _ = _synth_at_scale(20000)
pr = cProfile.Profile(); pr.enable()
run_rows(rows, dataset='b', check_replay=False)
pr.disable(); pstats.Stats(pr).sort_stats('tottime').print_stats(10)
"
```

Two traps this codebase has already hit, worth knowing before you add code:

1. **`list.remove(entry)` on a pydantic model** is a linear scan of `__eq__`
   calls. Track completion in a `set` of ids instead.
2. **Scanning all entries per entry** (e.g. "is there a row near this amount")
   is O(n²). Pre-index — sorted amounts plus `bisect`, or a dict.

## Working on the frontend

`web/` is deliberately buildless — no bundler, no framework, no `node_modules`.
It is native ES modules and hand-written CSS: edit a file under `web/js/` or
`web/styles/` and reload. The server sends `Cache-Control: no-cache` on assets,
so a stale module cannot silently survive a change.

Where things live:

| Want to change | Edit |
| --- | --- |
| a colour, a type size, a radius, a duration | `web/styles/tokens.css` |
| the app shell, the rail, responsive behaviour | `web/styles/base.css` |
| a badge, a table, the drawer, a dropzone | `web/styles/components.css` |
| what a screen shows | `web/js/views/<screen>.js` |
| a shared primitive (badge, empty state, drawer) | `web/js/ui.js` |
| an endpoint or an error message | `web/js/api.js` |

Fonts are self-hosted under `web/fonts/` (IBM Plex, latin subset, 222 KB). That
is deliberate: the app ships a `default-src 'self'` CSP with no external origin
allowed at all, and a reconciliation console may well run inside a locked-down
finance network where a font CDN is unreachable.

Keep it that way unless there is a real reason not to: a build step is a
dependency, a lockfile, and a way for the demo to break on someone else's
machine.

## Conventions

- Money is **integer paise** everywhere. Never floats — `_to_paise()` at the
  boundary, formatting only at the edge.
- Every match and every refusal writes an `AuditRecord` with a human-readable
  rationale containing the actual arithmetic.
- Rules are versioned (`rule-name@v1`) so a past decision can be traced to the
  logic that made it.
- Line length 100, `ruff` with `E,F,I,UP,B,SIM`.
