# Anomaly catalog

Anomalies are injected **after** the clean baseline passes validation (stage
`POST_BASELINE_VALIDATION`). Each injector:
* changes only its own target records;
* draws targets from its own RNG stream, disjoint from the other injectors;
* writes one ground-truth row per target, with the original and injected values.

| Issue type | Count (starter / full) | Injection | Detector rule | Detection logic | Decision | Effect in forecasting |
|---|---|---|---|---|---|---|
| BALANCE_MISMATCH | 5 / 25 | Shift a bank statement's `closing_balance_minor` by 50–25,000 USD equivalent (sign random) | BAL-001 | Recalculate closing = opening + Σ ledger and compare it with the statement | HELD_FOR_REVIEW | Runs whose cutoff is after the break stay EXECUTED (not approved); the ledger balance is used |
| FX_RATE_MISSING | 3 / 10 | Delete one (currency, business day) rate, at least 5 days inside the range | FX-001 | Every non-USD account currency needs a rate for every business day; the expected `rate_id` is reported | HELD_FOR_REVIEW | Conversion uses the documented STALE_FALLBACK rate and is flagged; never rate 1 |
| DUPLICATE_CASH_FLOW | 5 / 20 | Copy a forecast item under a new `CF-` id, with a second `forecast_source_link` to the same source | DUP-001 | Two signals: total `allocation_bp` per (source_type, source_id) > 10,000, **or** more than one item for the same (source_type, source_id, instalment_no), which catches duplicates even when they have no bridge row. Survivor order: the item that already received settlement, then (known_at, cf_id). Severity MEDIUM when the duplicate is linked, HIGH when it has no link | AUTO_RESOLVED | The duplicate is EXCLUDED from every run, including runs after the original has been settled. The run guard judges duplicates against every item known before the cutoff, using only settlements recorded before it |
| CUTOFF_LEAKAGE | 4 / 15 | **Back-dated revision:** append a REESTIMATE revision claiming to be known 1 hour after the item, whose values and basis are copied from the item's own future settlement: `basis_actual_id`, `expected_date` and `amount_minor` copied from an actual recorded after the item was known (and after at least one cutoff) | CUT-001 | The basis actual's `recorded_at` ≥ the item's `known_at`. The per-run guard uses ≥ cutoff_ts | REJECTED | EXCLUDED in runs whose cutoff precedes the basis; its actual is attributed to ANOMALY |
| MIXED_CURRENCY | 4 / 15 | Restate a forecast item in another currency (amount converted, currency code changed) while it stays on its original account | CCY-001 | Item currency ≠ account currency (the same check covers ledger, actual, receivable and payable rows) | REJECTED | EXCLUDED, so it is never summed with amounts in the account currency; its actual is attributed to ANOMALY |

## Look-ahead rules on the revision history

* **CUT-001:** a basis actual recorded at or after the item, **or any of its revisions**, became known. One finding per item. The per-run guard excludes the line when the version in force at the cutoff has such a basis.
* **CUT-003:** the item's current values match **none** of its recorded revisions. That's an untracked overwrite: the change can't be dated, so it's held for review. Forecasts are unaffected because they read revisions only.
* **Not flagged:** a CUSTOMER_PROMISE revision recorded before the settlement, even when its date turns out close to the actual payment (legitimate foresight). Tests: `tests/test_revision_history.py`.

## Ledger entries in the wrong currency (not a planted anomaly, handled safely)

A ledger entry whose currency differs from its account's is reported by CCY-001 (`affected_table = ledger_entry`, REJECTED). It is **quarantined**: excluded from balances, forecast opening balances and variance, and never summed. The resulting difference against the bank statement is marked `RECONCILING_ITEM`, not `BREAK`, so BAL-001 doesn't raise duplicate alerts. The pipeline keeps running (`tests/test_currency_quarantine.py`).

## Negative and boundary cases (must not be flagged)

* **Same amount and date on different sources**: `legit_same_amount_pairs` invoices with the same customer, amount, account and dates. DUP-001 compares only inclusions of the *same* source.
* **Instalment plans**: one receivable with two forecast items at 5,000 bp each, totalling exactly 10,000. They have different `instalment_no` values, so the item-level rule never groups them.

## Related validation checks

| Check | Rule | Exceptions |
|---|---|---|
| `BUS-DUPSRC` | Allocation per source ≤ 10,000 bp | Reported by DUP-001 |
| `BUS-DUPITEM` | One forecast item per (source_type, source_id, instalment_no) | Reported by DUP-001 |
| `BUS-CF-LINK` | Every forecast item has a `forecast_source_link` row pointing to its own source | Logged as VALIDATION, `BUSINESS_RULE_VIOLATION`, HIGH, HELD_FOR_REVIEW |

Regression tests: `tests/test_duplicate_regression.py`. They cover an unlinked duplicate (detected
as HIGH, excluded, projections unchanged), a missing or mismatched source link, a duplicate of an
item settled before a later cutoff, a duplicate known earlier than its settled original, and
legitimate instalments and same-amount items.
* **Payroll basis**: payroll items legitimately use the previous month's actual, recorded before `known_at`.
* **Late-posted entries**: value-dated before a cutoff but recorded after it. They are excluded from the opening balance and are not leakage.
* **Weekend and holiday rates**: PRIOR_BUSINESS_DAY is normal and is not FX_RATE_MISSING.

On the clean baseline the detectors produce 0 findings. This is reported as `baseline_false_positives` in `evaluation_summary.json`.

## Ground truth

`output/<scale>/ground_truth/ground_truth.{csv,json}` has the columns `anomaly_id`, `issue_type`,
`affected_table`, `affected_record_id`, `affected_field`, `original_value`, `injected_value`,
`expected_detection_rule`, `expected_decision`, `injection_stage` and `notes`.

It is never written into the dataset folders and never passed to the detector or the engine.
Only `engine/evaluation.detection_metrics` reads it, to compute TP, FP, FN, precision, recall and
F1 per type. Matching uses `(issue_type, affected_record_id)`.
