# Business rules

Rule IDs appear in code docstrings, `variance_attribution.rule_id` and `exception_log.rule_id`.

## Money and FX

| ID | Rule |
|---|---|
| MNY-01 | Amounts are signed integers in minor units of their currency (USD/EUR/GBP/INR 2 dp, JPY 0 dp). Positive = inflow/credit. |
| MNY-02 | Rounding is ROUND_HALF_EVEN, done with exact integer arithmetic (`core/money.div_half_even`). |
| MNY-03 | Amounts in different currencies are never added. Aggregation inside an account or currency is guarded (`assert_single_currency`, ledger currency check). Cross-currency totals exist only in the reporting currency (USD). |
| FX-01 | Quotation: `rate` = USD per 1 unit of `base_currency`, 8 dp. USD amounts are never converted (IDENTITY). |
| FX-02 | Rates exist for business days (Mon–Fri excluding configured holidays), including the last business day before the range, so 1 January converts. **Hybrid mode:** real rates from the pinned Kaggle extract (must cover every required business day, otherwise generation stops). **Synthetic mode:** business-day GBM from the configured starting levels, plus configured shocks. |
| FX-03 | Business day d uses the rate for d (EXACT). A non-business day uses the latest prior business-day rate (PRIOR_BUSINESS_DAY). |
| FX-04 | A missing business-day rate falls back to the most recent earlier rate within 5 business days (STALE_FALLBACK). It is raised as FX_RATE_MISSING and held for review. Beyond that window the status is MISSING, the amount stays unconverted and is excluded from consolidation (counted). A rate of 1 is never substituted. |
| FX-05 | Forecast FX: the last rate published before cutoff (effective for cutoff − 1 day), times (1 + shock_bp / 10,000) for the scenario. |
| FX-06 | Conversion: `round_half_even(amount × rate_e8 × 10^dst_dp / (10^8 × 10^src_dp))`, done per row. |
| FX-07 | Generated USD-denominated amounts are expressed in local currency at the effective rate of their date. Dates before the range use the first published rate (2025-12-31). |

## Generation (synthetic source data)

| ID | Rule |
|---|---|
| ENT-01 | The first five entities are incorporated in US, DE, GB, IN and JP, so all five functional currencies exist. The rest are drawn by country weights. Planned monthly revenue is lognormal (median 2,000,000 USD, σ = 0.7), clipped to [0.3M, 15M]. |
| ENT-02 | Minimum liquidity policy = `liquidity_policy_months` (1.5) × monthly revenue, in USD cents. Raised from 0.5, at which entities' lowest projected cash was a median 8× the minimum and only the STRESS one-off payment could breach it. |
| ACC-01 | Every entity has at least OPERATING, COLLECTION and DISBURSEMENT accounts. Extra accounts (up to 8 in total) are allocated by √size. OPERATING and PAYROLL accounts use the functional currency; others are foreign with 40% probability. All five currencies are guaranteed. |
| ACC-02 | Dormancy applies only to RESERVE accounts, on a business day in the dormant window. There is no ledger activity on or after the dormancy date. |
| RCV-01 | due_date = next business day on/after invoice_date + terms (30/45/60 days). |
| RCV-02 | Payment delay = round(Gamma(shape, scale)) + shift, by risk class. Payment falls on the next business day on/after due + delay. Intercompany receivables settle on due_date. **Synthetic profile:** A (1.2, 2, −1), B (2, 5, 0), C (2, 12, 0), mix 50/35/15. **Calibrated profile (default):** see RCV-08. |
| RCV-08 | Classes A and B use the parameters fitted from real B2B invoices (`data/external/ar_delay_calibration.json`): A (1.089, 4.429, −4), B (0.387, 18.935, −2), mix A/B ∝ observed invoice shares (≈ 83% / 7%). Class C (absent from the data) keeps (2, 12, 0) at `calibrated_class_c_weight` = 10% as a stress segment. |
| RCV-03 | Never-pay probability by class (A 0%, B 1.5%, C 6%). OVERDUE on due + 1 day; WRITTEN_OFF on due + 60 days (if within range). |
| RCV-04 | 4% of single invoices are paid in two parts: 60% first, the remainder 15 days later. |
| RCV-05 | Instalment receivables (class A) have two forecast items at 50/50 with due dates 30 days apart. |
| RCV-06 | OVERDUE is set only while nothing has been paid. A partially paid receivable stays PARTIALLY_PAID until fully paid. |
| RCV-07 | Boundary case: `legit_same_amount_pairs` invoices share customer, amount, account and dates with another invoice. They are legitimate and must not be flagged as duplicates. |
| PAY-01 | Supplier scheduled_date = first Thursday payment run on/after invoice + terms. |
| PAY-02 | Approval is 2 business days before the scheduled date (never before the invoice date). |
| PAY-03 | Suppliers are cancelled with 1.5% probability, 1 business day before schedule. Otherwise they are paid on schedule, or 1–2 business days late with 5% probability. |
| PAY-04 | Payroll is paid on the 25th (prior business day if needed). The forecast amount equals the previous month's actual payroll (`basis_actual_id`); January uses the budget. The actual is budget × (1 + N(0, 1.5%)). |
| PAY-05 | Rent is fixed, paid on the first business day of each month, and known since 2025-12-01. |
| PAY-06 | Tax is paid on the configured dates (next business day): 4% of quarterly revenue × (1 + N(0, 5%)), known 30 days ahead. |
| PAY-07 | One-off payments (CAPEX, dividend, legal settlement, acquisition deposit) are 0.5–2.5 × monthly revenue, known 20–45 days ahead and paid on schedule. |
| PAY-08 | An intercompany payable mirrors its receivable: same amount, currency and date, with an account in the debtor entity in that currency. |
| LED-01 | Settlement entries are posted for every receivable/payable payment within the data range. |
| LED-02 | Monthly bank fee per active account on the last business day (15–250 USD equivalent). |
| LED-03 | Sundry receipts and payments fill the configured ledger count exactly. They are small tickets (lognormal, median 0.06% of monthly revenue, capped at 1%), weighted by account type and √size, roughly balanced between receipts and payments, and follow a weekday profile (Mon 1.15, Tue 1.0, Wed 0.95, Thu 1.0, Fri 1.2) with ×1.4 activity on the last two business days of each month. |
| LED-04 | Pooled accounts open at their sweep target. Every other account opens at buffer(account type × monthly revenue) + max(0, −min cumulative flow including sweeps), rounded up to 1,000 units. The clean baseline therefore never goes negative. |
| LED-07 | **Cash pool.** Each entity's COLLECTION and DISBURSEMENT accounts in its functional currency are pooled under its OPERATING account (`sweep_parent_account_id`). On the last business day of every Mon–Sun week, each pooled account is swept back to `sweep_target_minor`, defined as 0.05 × monthly revenue plus its largest intra-week drawdown. The difference moves to or from the OPERATING account as an INTERNAL_TRANSFER_OUT / INTERNAL_TRANSFER_IN pair, posted the same day. Interest and a per-sweep bank fee (2–10 USD equivalent) are booked before that day's sweep, so a week without activity still sweeps a non-zero amount. PAYROLL, RESERVE and foreign-currency accounts aren't pooled. Sweep slots are fixed in advance (pooled accounts × weeks × 2), so ledger volume stays exact. |
| LED-05 | Monthly interest = half_even(month-end balance × bp × days / 365 / 10,000), where the balance includes interest already credited. |
| LED-06 | 5% of non-sweep entries post on the next business day (never beyond range end). `recorded_at` = posting date 18:00Z. |
| CF-01 | Forecast items are created from receivable instalments and payables. `known_at` = invoice/known date 09:00Z. IDs follow sort order. |
| CF-02 | Payroll item `basis_actual_id` = previous month's payroll actual, recorded before `known_at`. |
| CF-03 | Item status is derived from settlements: SETTLED / PARTIALLY_SETTLED / OPEN; CANCELLED or WRITTEN_OFF from the source status. |
| CF-04 | **Revision history.** Every forecast item has an append-only version history in `cash_flow_forecast_revision`. Revision 1 holds the original values, known at the item's `known_at`. About 6% of single-instalment customer receipts get a CUSTOMER_PROMISE revision (a new expected date), recorded strictly after the item became known and strictly before its first settlement was recorded. The `cash_flow_forecast` row always equals the latest revision. |
| BNK-01 | **Independent bank model.** The bank's transaction feed (`bank_statement_line`) is generated from the economic cash events by bank rules. It never reads ledger rows. Most events are booked on their value date for the same amount. |
| BNK-02 | 5% of customer and sundry receipts clear 1–2 business days later at the bank. A line that would be booked after the data end is not on the statements yet. |
| BNK-03 | 4% of outgoing supplier, intercompany and one-off payments carry a wire charge (5–40 USD equivalent) booked by the bank on the same day. The ledger never records it. |
| BNK-04 | Statements (`account_balance_statement`) are built only from bank lines: opening = account opening balance; closing = opening + credits − debits by booking date. |

## Positions and forecasting

| ID | Rule |
|---|---|
| BAL-01..04 | See `engine/balances.py`: closing = opening + credits − debits, continuity, conversion and statement reconciliation. |
| BAL-02 | A ledger entry whose currency differs from its account's is **quarantined**. It is excluded from balances, forecast opening balances and variance attribution, never summed, and logged as CCY-001 (REJECTED). The pipeline continues. |
| REC-01 | Each valid ledger entry matches one bank line on the same account and exact amount, booked on its value date or up to 3 business days later. Matching is one-to-one, in (ledger date, lag, ids) order. |
| REC-02 | Item statuses: MATCHED; TIMING_DIFFERENCE (bank later); BANK_ONLY (bank charge, or the bank side of a currency-quarantined entry); LEDGER_ONLY (in transit: clearing window runs past the data end); UNMATCHED_BANK / UNMATCHED_LEDGER (no match and no documented reason, a genuine break). Every ledger entry and bank line appears in exactly one item (`bank_reconciliation_item`). |
| REC-03 | Daily: explained difference = open BANK_ONLY − LEDGER_ONLY − in-flight TIMING amounts. A day is RECONCILED when statement − ledger = 0, RECONCILING_ITEM when it equals the explained amount, and BREAK otherwise (BAL-001). UNMATCHED items are never explained. |
| FC-01 | cutoff_ts = cutoff date 00:00Z. Records are used only when `recorded_at` / `known_at` < cutoff_ts. |
| FC-02 | Run opening balance = account opening + ledger entries recorded before cutoff, excluding quarantined entries (BAL-02). |
| FC-10 | Forecast runs project the weekly cash-pool sweeps with the LED-07 rule, using the opening balance known at cutoff and the run's own lines (`forecast_run_line.source_type = SWEEP`). |
| FC-11 | A forecast run reads each item's **latest revision known strictly before the cutoff** (`forecast_run.items_as_of`). Later revisions can't change an earlier run. `forecast_run_line.revision_id` records the version used. |
| FC-12 | **Run-rate estimate** for recurring flows that have no forecast item at the cutoff (`forecast_run.run_rate_lines`, `source_type = RUN_RATE`). Payroll not yet booked: the account's last payroll actual recorded before the cutoff, on the next pay day (25th, previous business day) in the horizon, skipped when a payroll item for that month is already known. Small flows (sundry receipts and payments, bank fees): the account's net over the 56 days before the cutoff divided by the business days in that period (half-even), on every business day of the horizon. Only entries recorded before the cutoff are used, and the lines are identical in every scenario (checked by `BUS-CUTOFF-RUN` and `BUS-SCN`). Configurable under `forecast.run_rate`. |
| FC-03 | Candidate items are known before cutoff and not cancelled or written off before cutoff. Remaining = amount − settlements recorded before cutoff; any settlement closes a payroll item. |
| FC-04 | Overdue unsettled items are re-dated: receivables to cutoff + 7 days, payables to cutoff (next business day). |
| FC-05 | Guards: CUTOFF_LEAKAGE, MIXED_CURRENCY and DUPLICATE_CASH_FLOW lines are EXCLUDED with a reason and logged. A duplicate is a second item for the same (source_type, source_id, instalment_no), or a link that pushes a source's allocation above 100%, whether or not the item has a source link. It is judged against every item known before the cutoff, including settled ones. The surviving item is the one with settlements recorded before the cutoff, otherwise the earliest (known_at, cf_id). |
| FC-09 | Every forecast item must have a `forecast_source_link` row to its own source (validation `BUS-CF-LINK`; failures are held for review). |
| FC-06 | Scenario transformation (see `scenarios.md`). Lines pushed beyond the horizon become OUT_OF_HORIZON. |
| FC-07 | Horizon is 30 days. Cutoffs are 1 March, April, May and June 2026; each cutoff has BASE, DELAYED_RECEIPT and STRESS runs, plus configured re-runs. |
| FC-08 | Re-runs get the next sequence number for that cutoff. The old run moves to SUPERSEDED and is retained. |
| RUN-01 | A run is APPROVED automatically unless one of its accounts (a) converted its opening balance with a STALE_FALLBACK/MISSING FX rate, or (b) has an unexplained statement BREAK dated within `approval_policy.balance_break_lookback_days` (7) before the cutoff and at least `balance_break_materiality_reporting_minor` (1,000.00 USD at that day's rate; unconvertible breaks count as material). Held runs stay EXECUTED, and each hold reason is written to the audit trail (`FORECAST_RUN_HELD`). Older breaks don't hold a run, because its opening balance comes from the ledger. `engine/approval.py`. |
| ALR-01..03 | Liquidity alerts (see `engine/liquidity_alerts.py`): breach when projected closing < policy minimum; CRITICAL when the minimum is negative. Group threshold = sum of entity minimums. |

## Variance attribution

| ID | Category | Rule |
|---|---|---|
| VAR-TIM | TIMING | Included item: −f at the forecast date, +f at the first actual date. Also covers an actual for an item known before cutoff but forecast outside this window. |
| VAR-AMT | AMOUNT | Included item: + actual amounts, −f at the first actual date. Items that will never settle (written off, cancelled, or scenario-only): −f at the forecast date. |
| VAR-UNF | UNFORECAST | Ledger entries without a forecast item, or items known after cutoff, grouped per account and day (source = `account|date`), minus the FC-12 run-rate estimate for them (source type `RUN_RATE_ESTIMATE`, one row per estimate line). What remains is the part the estimate didn't anticipate. |
| VAR-ANO | ANOMALY | Actuals of items the detector flagged, which were therefore excluded from the run. |
| VAR-AMT (pool) | AMOUNT | Cash-pool sweeps, projected (`SWEEP` lines) and actual (INTERNAL_TRANSFER entries), with source `CASH_POOL_SWEEP`. They reallocate variance within a pool and net to zero per pool. |
| VAR-FX | FX_MOVEMENT | A × rate(d) − A × run rate (reporting currency). |
| VAR-RES | RESIDUAL | Reporting variance − Σ converted components − FX. Only per-component rounding; |residual| ≤ 6 minor units. |

Late-posted entries (value date before cutoff, recorded after) take effect at the cutoff date.
In local currency the identity `variance = timing + amount + unforecast + anomaly` is exact.

## Exceptions and decisions

| Decision | Meaning | Resolution status |
|---|---|---|
| AUTO_RESOLVED | System applied the documented fix (duplicate excluded) | RESOLVED |
| HELD_FOR_REVIEW | Needs a reviewer (balance break, stale FX, structural failure) | OPEN |
| REJECTED | Record not usable as is (leakage, mixed currency); excluded from forecasts | OPEN for detection, RESOLVED for per-run guard rows |

Source records are never modified to hide a failure.

## Evaluation conventions

* Accuracy metrics use matching (run, account, date) rows only (inner join). Rows with no actual or no usable FX rate are excluded and counted. Superseded runs are excluded.
* error = forecast − actual. Bias > 0 means over-forecast. Figures are in USD major units.
* Percentage error is computed only where |actual| ≥ 1,000.00 USD (avoids near-zero denominators).
* When precision or recall has a zero denominator, the metric is reported as null.
