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

**Do not reach for the `4111 1111 1111 1111` test card first.** It is an
international Visa number, and an India-domestic test account rejects it with
*"International cards are not supported"* — which reads like a broken
integration and is not one.

Use a method with no card at all. Create a payment link (Dashboard → **Payment
Links** → **Create Payment Link**, or the API), open it, then pay by either:

- **Netbanking** — pick any bank and Razorpay shows a test simulator with
  **Success** / **Failure** buttons. This is the path that worked here.
- **UPI** — enter the test VPA `success@razorpay`.

Docs: <https://razorpay.com/docs/payments/payments/test-card-details/>

Four or five payments is plenty. Be aware of what that does and does not buy
you: **test mode issues payments but no settlements**, so those payments have no
payout side to match against and reconcile to a 0% auto-match rate with every
row a reasoned exception. That is the engine being correct about incomplete
data. Two payments of the same amount on the same day will also raise a
`duplicate` — the detector doing its job on genuinely distinct payments, which
is the safe direction.

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

**Verified.** `scripts/verify_razorpay.py` exits 0 against a live test-mode
account. Credentials load, the `rzp_test_` prefix is confirmed, the API connects
with `provenance = live_test`, and the secret is never printed — only a masked
key hint.

Measured on the account as it stands:

| | |
| --- | --- |
| Pulled | 4 payments, 0 refunds, 0 settlements |
| Reconciled | 4 entries → 0 groups, 4 exceptions |
| Auto-match rate | 0.0% |
| Gross processed | ₹27,500.50, all of it in exception |
| Replay | stable |
| Persisted | run `225dbe1a68da`, visible to all 5 console endpoints |

**The 0% is correct.** Razorpay test mode issues payments but no settlements, so
there is no payout side to match against. Each payment becomes a
`missing_in_bank` exception with a stated reason, plus one `duplicate` where two
payments share an amount and a date — the duplicate detector firing on genuinely
distinct payments, which is the safe direction: it raises a question for a human
rather than silently merging them.

So this integration proves the **ingestion path** against data Razorpay
generated — authentication, the test-mode guard, epoch parsing, integer paise,
provenance labelling, persistence, and the UI reading it back. It does not
demonstrate a closed four-way loop, and the README does not claim it does.

In the video, the accurate sentence is: **"This is Razorpay's own test-mode
data, not data I generated."** Do not extend that to the loop — the bundled
datasets are what close it, and they are the honest place for the accuracy
numbers.
