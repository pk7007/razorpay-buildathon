---
name: Reconciliation scenario
about: A real-world case the engine does not model yet (FX, chargebacks, partial refunds…)
title: 'scenario: '
labels: enhancement
assignees: ''
---

## The scenario

<!-- e.g. "an international card payment settles net of an FX markup, so
     gross - fee - GST no longer equals the settlement net" -->

## Which identity it breaks

The engine matches on accounting identities (see `docs/ARCHITECTURE.md`). Which
one stops holding, and why?

## What signal would resolve it

Is it decidable from the four batch files alone, or does it need another source
(e.g. Razorpay's settlement recon report as a join key)? If it is not decidable,
the right outcome may be a **new exception category**, not a new match rule.

## Suggested benchmark

Scenarios are only fixed once they are measurable. Sketch what
`synth.py` would need to emit, and what `labels.json` / `truth.json` should say
the correct answer is.
