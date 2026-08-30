# The LLM resolver

**The product does not need this.** Every published number in this repo — the
held-out precision, the recall, the throughput — was produced with the LLM
switched off. This page is about verifying the optional path, and about being
straight regarding what it contributes.

---

## Where the model sits

Reconciliation is mostly exact arithmetic, and exact arithmetic does the work.
The model is only allowed near the **residual tail**: entries that no accounting
identity can place, because they share no identifier with anything.

The realistic case is an **off-gateway transfer** — a customer pays by direct
NEFT, finance books it, and the bank narration and ledger memo name the same
counterparty but share no reference. Amount, date and counterparty text are the
only signal. That is worth a model. A UTR match is not.

> An LLM should never decide where your money went. It should only make a
> suggestion that a rule then checks.

## What bounds it

| Bound | Value | Why |
| --- | --- | --- |
| Temperature | `0` | the same batch must reconcile the same way twice |
| Timeout | `20s` | a hung call cannot hold a reconciliation open |
| Retries | `2` | handled by the SDK, then it gives up |
| Residual cap | `150` entries | past this it is expensive and worse than the scorer |
| Confidence floor | `0.72` | below it, the proposal is refused and logged |
| Arithmetic check | always | amounts must reconcile within 5%, whatever the model claims |
| Id check | always | a proposal naming an entry that was not sent is refused whole |
| Conservation | asserted | every entry ends in exactly one group or one exception |

The last three are the important ones. **Confidence is an opinion; the
arithmetic is a fact, and the fact wins.** A model that returned
`{"confidence": 1.0}` for a ₹0.01-and-₹90,000 pairing gets refused, and the
refusal is written to the audit trail.

Untrusted narration text is flattened (newlines stripped, truncated to 120
chars) before it enters the prompt, and the system prompt states that the user
message is data, never instructions. A bank narration is the one field an
outsider can write into.

---

## Verify it

### Without a key (costs nothing)

```bash
python scripts/verify_llm.py --dry-run
```

Shows the exact payload that would be sent — including the redaction applied to
narration text — and runs the deterministic baseline so you can see what the
model would have to beat.

### With a key

1. Get a key at <https://console.anthropic.com> → API Keys
2. Add it to `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5
```

3. Install the optional SDK and run:

```bash
pip install anthropic
python scripts/verify_llm.py
```

One real call. It reports:

- the token counts **the API returned**, and the cost computed from them
- latency
- what the model grouped, and its stated reason
- every proposal the engine **refused**, and why
- a diff against the deterministic heuristic on the identical input

That last section is the point. It answers *"does the model earn its place?"*
with evidence — and if the answer is *no difference on this input*, the script
says exactly that rather than dressing it up.

Cost is tiny: a typical residual is a few dozen entries, so one call is
~1,500 tokens, well under a cent. The script extrapolates a per-1,000-entry
figure so the number is meaningful at scale.

---

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | *(unset)* | unset ⇒ heuristic resolver, silently and safely |
| `LLM_MODEL` | `claude-sonnet-5` | |
| `LLM_TIMEOUT_SECONDS` | `20` | |
| `LLM_MAX_RETRIES` | `2` | |
| `LLM_INPUT_USD_PER_MTOK` | `3.0` | change with the model, or the cost figure lies |
| `LLM_OUTPUT_USD_PER_MTOK` | `15.0` | |
| `RESOLVER_ACCEPT_THRESHOLD` | `0.72` | |

Point it at a cheaper model and update both price variables — the reported cost
is computed from them, and a stale price is a wrong number in the audit trail.

---

## Failure is a feature

Pull the key mid-demo:

```bash
# with a key set, then:
unset ANTHROPIC_API_KEY && python scripts/run_reconciliation.py --dataset messy
```

Everything still works. `resolve()` catches **any** exception from the model
path — timeout, connection reset, malformed reply, a truncated JSON body — logs
the failure with its exception type, and hands the residual to the deterministic
scorer. The run completes. The numbers stand.

`GET /api/health` reports which backend is live (`llm` or `heuristic`), and the
dashboard shows it in the header, so it is never ambiguous which produced a
result.

---

## What is verified, and what is not

**Verified** (`tests/test_llm_live.py`, 30 tests, run against a stand-in for the
SDK): that the bounds actually reach the client, that cost is computed from
reported tokens rather than estimated, that hallucinated ids and
non-reconciling amounts are refused, that an entry cannot be claimed twice, that
the oversized-residual cap is enforced before a client is even constructed, that
malformed and truncated replies yield nothing rather than crashing, and that
every failure mode degrades to the heuristic.

**Not verified:** that a real model returns *useful* groupings. No API key was
available while this was built, so the live branch has never been executed
against Anthropic. `scripts/verify_llm.py` closes that in one command.

Until it is run, the honest claim is the one the README makes: the LLM path is
contract-tested but unverified live, and **every number published here was
produced without it**. That is the floor, not the ceiling — which is a stronger
position to argue from anyway.
