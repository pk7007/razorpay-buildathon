# The demo month

Thirteen designed cases and four designed failures, in one batch small enough
to read end to end — 55 records, over the 50-record bar the brief sets. Every
row exists to demonstrate one thing, and the expected outcome for every row is
known in advance — `scripts/demo_reset.py` asserts all of them, so a demo
never runs on a result nobody checked.

This is **synthetic data in the shape of real exports**. It is not Razorpay
data and is not labelled as such anywhere in the product.

## What each case is for

The last four are deliberately ordinary. A batch of nothing but edge cases is
its own kind of dishonesty: it hides whether the plain path still works, and it
makes an auto-match rate meaningless. These four tie across all four sources
with no drama, each on a different fee shape and a different date format.

| Rows | Case | What it proves |
| --- | --- | --- |
| `pay_norm` `setl_norm` `bank_0` `L-1` | An ordinary card sale | Four-way tie: gross → fee + GST → payout → book |
| `pay_upi` `setl_upi` `bank_1` `L-2` | Zero-MDR UPI | No global 2% is assumed; a zero fee is a real fee |
| `pay_flat` `setl_flat` `bank_2` `L-3` | Flat-fee netbanking, ₹2,500.50 | Per-method rate cards, and decimals that do not divide evenly |
| `pay_big` `setl_big` `bank_3` `L-4` | ₹1.25 crore with TDS withheld | Integer paise at scale; TDS as a term in the identity |
| `pay_refund` `rfnd_part` `setl_refund` `bank_4` `L-5` | Partial refund inside the payout window | The payout is net of the refund, and still ties |
| `pay_full_refund` `rfnd_full` `L-6` | Fully refunded sale | No payout is *expected* — absence is the correct answer |
| `pay_cb` `cbk_1` `setl_cb` `bank_5` `L-7` | Chargeback lost after payout | A clawback is a financial event, not noise |
| `pay_late` `setl_late` `bank_13` `L-8` | Sold 28 Jul, paid 2 Aug | The month boundary is not a wall |
| `pay_never` `L-9` | Booked, captured, never settled | The money you actually have to chase |
| `pay_wallet` `setl_wallet` `bank_9` `L-10` | Wallet sale, ₹15,000 | A fourth instrument with its own rate |
| `pay_upi2` `setl_upi2` `bank_10` `L-11` | Second zero-MDR UPI, `dd-mm-yyyy` | Zero fees are not a one-off fixture |
| `pay_nb2` `setl_nb2` `bank_11` `L-12` | Flat fee on ₹4,200, `dd/mm/yyyy` | ₹18 + ₹3.24 GST — a fee that is not a percentage |
| `pay_tds` `setl_tds` `bank_12` `L-13` | ₹22,000 with 1% TDS, `13 Jul 2026` | TDS at ordinary scale, not just at a crore |

## The four exceptions it must raise

| Row | Category | Why |
| --- | --- | --- |
| `bank_6` | `duplicate` | UTR700006 exported twice, same amount, same day |
| `bank_7` | `missing_in_ledger` | ₹1,500 IMPS credit with no reference and no book entry |
| `pay_zero` | `missing_in_bank` | A ₹0.00 capture — the amount most likely to "fit" anywhere |
| `bank_8` | `fee_mismatch` | A ₹118 bank charge left the account with nothing explaining it |

## The ingestion traits

The files are deliberately awkward, because real ones are:

- **`bank.csv`** is HDFC-shaped: `Withdrawal Amt.` / `Deposit Amt.` split across two
  columns, Indian digit grouping (`1,21,00,000.00`), `dd/mm/yyyy` dates, and one
  torn row that must be quarantined with a reason rather than dropped.
- **`ledger.csv`** is Tally-shaped: `Sl No`, `Particulars`, `Amount INR`.
- **`payments.csv`** carries four date formats in one column, because exports
  concatenated from two systems really do arrive like that.

No column mapping is configured anywhere. The engine detects all of it, and
refuses rather than guessing when two columns are equally plausible.
