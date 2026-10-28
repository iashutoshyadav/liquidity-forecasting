# Design decisions and trade-offs

This page explains the main choices behind the system, the options I rejected, and what each choice costs.
The rule-level detail is in [business_rules.md](business_rules.md). The structure is in [architecture.md](architecture.md).

## The decisions

| # | Decision | Why | Rejected alternative | What it costs |
|---|---|---|---|---|
| 1 | **Money as integers in minor units**; FX rates as integers × 10⁸; half-even rounding | Reconciliation must be exact to the cent. With integers, `opening + credits − debits = closing` holds exactly on every row, and variance causes add up exactly | Floats (drift: `0.1 + 0.2 ≠ 0.3`); `Decimal` (correct, but slow and easy to mix with floats by accident) | Conversion code is explicit, and every currency needs its minor-unit count |
| 2 | **One schema module** generates the data dictionary, the SQL DDL and the validators | Docs, database and checks can't drift apart | Hand-written docs and DDL | Adding a field means touching the schema first, which is deliberate friction |
| 3 | **Synthetic core plus pinned real data** (Kaggle FX rates, a payment-delay calibration) | No public dataset holds a company group's treasury records. Synthetic data gives exact volumes, valid joins and a known answer key; real data adds realism where the licence allows | A public dataset only (4 were assessed and rejected: balances don't reconcile, restrictive licence, and so on) | Results show the system works on realistic data, not on one real company's data |
| 4 | **Rules, not machine learning**, for detection and forecasting | Treasury decisions need a reason a reviewer can check. Every finding points to a rule and a record, and every forecast line to a source item | ML anomaly detection or ML forecasting (on synthetic data it would mostly learn the generator, and it can't explain itself) | The rules cover the anomaly types they were written for; the perfect scores don't show they generalise |
| 5 | **Strict cutoff by when information was recorded**, plus an **append-only revision history** for forecast items | A forecast for 1 March may only use what was known before 1 March. Without versions, a later change to an item could silently rewrite an earlier forecast | Filtering on business dates only; overwriting items in place | An extra table, and a redundant "current" item row that validation (`BUS-REV`) must keep equal to the latest revision |
| 6 | **The bank side is modelled separately** from the ledger, with clearing delays and bank-only charges | Reconciliation is only meaningful if the two sides can disagree. Every difference is either explained (timing, charge, in transit) or reported as a break | Statements derived from the ledger, which can never break | Both sides start from the same economic events; bank errors and reversals aren't modelled |
| 7 | **Variance split into causes that add up exactly** (timing, amount, unforecast, bad data, FX movement, rounding) | "The forecast was off by 3M" is useless on its own. Each cause points to forecast items, ledger entries or account-days | One total error figure; statistical error bands | Small flows are grouped per account and day, so they're traceable to a day, not to a single entry |
| 8 | **Scenarios may differ only through five documented assumptions**; which receipts are delayed is chosen by a stable hash | Every scenario difference is explainable and reproducible, and a check re-derives each scenario from BASE | Free-form scenario overrides | Scenarios are less flexible than a spreadsheet |
| 9 | **Approval holds only runs whose own inputs are affected** (RUN-01) | Holding every run for any open issue causes alert fatigue. A January break shouldn't block a June forecast | Hold everything while any exception is open (the original behaviour) | The scoping rule (7 days, 1,000 USD) is a policy choice and is configurable |
| 10 | **Run-rate estimate for recurring flows that have no forecast item yet** (FC-12) | Measuring first showed the biggest unforecast flow was payroll not yet booked at the cutoff, not sundry. A plain estimate cut BASE error per account from 7.1% to 6.1% and unforecast cash at day 30 by 82% | Leave it unforecast; a statistical time-series model | It's a plain average with no seasonality, and STRESS becomes more pessimistic |
| 11 | **Byte-for-byte determinism**: named random streams, fixed timestamps, IDs assigned after a stable sort, canonical CSV/JSON | "Run it again" must give identical files, so any change in output is a real change | Approximate reproducibility (same totals) | Extra care everywhere. It caught a pandas 3 difference in how missing values were written, which was fixed |
| 12 | **A static HTML dashboard**, not a web app | One file, no server, opens offline and can be published or attached. It reads only exported outputs, so it can't disagree with them | A Streamlit or React app with an API | No live data, logins or approval workflow; it's a report, not a tool |

## Things I learned by measuring rather than assuming

- **Unforecast cash wasn't mainly sundry.** It was mainly payroll that isn't booked by the cutoff. The fix targeted that first.
- **My first reconciliation matcher was too generous.** It explained a changed ledger amount as a pair of "bank-only" and "ledger-only" items. A test that edits one ledger amount now has to produce a break.
- **"Identical outputs" needed proof on a fresh machine.** A fresh clone with a newer pandas wrote missing values as `nan` in one file. Only that test found it.
- **An alert that never fires isn't being tested.** At a 0.5-month minimum, entities' lowest projected cash was a median 8× the minimum, so only the hypothetical STRESS payment ever raised an alert. Raising the policy to 1.5 months (a common level) gives BASE 2, DELAYED_RECEIPT 2 and STRESS 6 alerts, with days below the minimum rising 9 → 18 → 147. I didn't pick 1.75 months, even though it orders the alert counts neatly, because that would be tuning the policy to the result.
- **Approval policy matters as much as detection.** The original rule held every run, so every alert was noise.

## What I'd do next with more time

1. **Better run-rate:** add weekday and month-end profiles and trend; project interest from balances; estimate new invoices from recent invoicing.
2. **Dashboard drill-down to account level** and links from each top-10 contributor to its full record trail.
3. **Bank-side realism:** reversals, one payment covering several invoices, small FX rounding differences, and the matching rules for each.
4. **Measured detection for CUT-003** (untracked overwrites) with planted cases.
5. **Production path:** connectors for bank statement files (MT940 or camt.053) and ERP exports in place of the generator, a scheduler, CI that runs the reproducibility test, and an approval workflow for runs.
