# Connecting Razorpay test mode

Everything here is **copy-paste**. Total time once you have the keys: about five
minutes.

Why it is worth doing: right now this project reconciles data it generated
itself. Running it against data **Razorpay** generated turns *"it works on my
synthetic month"* into *"it works on the gateway's own output"* — which is a
completely different sentence in a panel, and closes the biggest credibility gap
in the submission.

---

## 1. Get test-mode keys

1. Sign in at **dashboard.razorpay.com**
2. Flip the top-left toggle to **Test Mode** (this matters — see the safety note)
3. **Settings → API Keys → Generate Test Key**
4. Copy the **Key Id** (`rzp_test_…`) and the **Key Secret** (shown once)

## 2. Put them in `.env`

```bash
cp .env.example .env
```

Then edit `.env`:

```
RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxxx
RAZORPAY_KEY_SECRET=your_secret_here
```

`.env` is gitignored and has never been committed. Verify any time with:

```bash
git check-ignore -v .env        # prints a rule if ignored; silent if NOT
```

## 3. Make sure the account has data

A brand-new test account is empty, and an empty account reconciles to nothing.
Create a few test payments first — Razorpay's test card:

```
card    4111 1111 1111 1111
expiry  any future date
cvv     any 3 digits
```

Docs: <https://razorpay.com/docs/payments/payments/test-card-details/>

Aim for **5–10 payments** and **1–2 refunds** — enough to show a real group, a
deduction, and an exception.

> Test-mode settlements often need triggering from the dashboard and may not
> appear. That is not a failure; the check below reports it and carries on.

## 4. Install the SDK

The SDK is an **optional** dependency — everything else works without it:

```bash
pip install razorpay
```

## 5. Run the check

```bash
python scripts/verify_razorpay.py
```

This is the whole point of the exercise. It walks six steps and reports
`PASS` / `FAIL` / `SKIP` for each, with the next action on any failure:

1. credentials present, and **test mode**
2. SDK installed
3. live pull — counts of payments, refunds, settlements
4. ingestion quality — usable vs quarantined rows
5. reconciliation — groups, exceptions, replay stability, conservation
6. what it found — the ₹ summary, the groups, the exceptions

It ends with a verdict. On success:

```
  VERIFIED against Razorpay test-mode data.
  This is data Razorpay generated, not data this repo generated.
```

To exercise the identical pipeline **before** you have keys:

```bash
python scripts/verify_razorpay.py --fixtures
```

## 6. See it in the product

```bash
python -m uvicorn finance_controller.api:app --port 8000
```

- `GET /api/razorpay/status` → `provenance_if_run` flips `fixture` → `live_test`
- `POST /api/reconcile/razorpay` → reconciles the live pull; the run is named
  `razorpay-live_test` and lands in the persistent worklist like any other

---

## Current verification status

The check has been run against a live test-mode account. Credentials load, the
`rzp_test_` prefix is confirmed, the SDK is installed, and the API connects with
`provenance = live_test`. The secret is never printed — only a masked key hint.

The one failing check is data: the account holds 0 payments, 0 refunds and 0
settlements, so a live run returns a batch labelled `razorpay-live_test`
containing zero records. It does **not** fall back to fixtures under a live
label, which is the behaviour that matters.

Razorpay test mode also does not issue settlements. Even a populated test
account therefore exercises the ingestion path rather than closing the four-way
loop — the bundled datasets are what demonstrate the loop, and the Razorpay
screen is what demonstrates the ingestion is real.

If you populate the account, say the sentence explicitly in the video: **"This
is Razorpay's own test-mode data, not data I generated."** Do not describe the
live path as closing the loop until settlements actually appear — overstating
this would cost more credibility than the gap itself.

---

## Safety

The integration refuses to run against anything but test mode:

```python
if not SETTINGS.razorpay_key_id.startswith("rzp_test_"):
    raise RazorpayUnavailable("refusing to run: key is not a test-mode key")
```

A live key would pull **real customer payment data** into a demo tool. There is
no flag to override this, deliberately.

Also true by design:

- `fetch_live()` **raises** rather than silently falling back, so fixture data
  can never be presented as live
- the secret is never printed, logged, or returned by any endpoint — the status
  endpoint shows only a 12-character prefix of the *public* key id
- `.env`, `*.key`, `*.pem` and `credentials.json` are all gitignored

## If it fails

| Message | Fix |
| --- | --- |
| `RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set` | `.env` missing or not loaded — check you are running from the repo root |
| `not a test-mode key` | You copied a live key. Switch the dashboard to Test Mode and regenerate |
| `razorpay package is not installed` | `pip install razorpay` |
| `API call failed: BadRequestError` | Key/secret mismatch — regenerate and copy both together |
| `The account returned no data` | Empty test account — create test payments (step 3) |
| `No settlements in this account` | Expected on many test accounts. Not a failure |

## What this does and does not prove

**Does:** the ingestion path handles Razorpay's real response shapes — epoch
timestamps, integer paise, prefixed ids, refunds keyed by `payment_id`, reported
fees treated as `actual` rather than re-estimated.

**Does not:** that the engine handles a *real merchant's* month. Test-mode data
is generated by you and is far tidier than production: no chargebacks in flight,
no bank charges, no FX, no partial-capture edge cases. That gap is stated in the
README and should stay stated.

`tests/test_razorpay_live.py` exercises this whole path against a stand-in for
the SDK, so the code is covered whether or not credentials exist.
