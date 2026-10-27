# Liquidity Forecasting and Risk Monitoring System

This system consolidates bank balances and expected cash flows across 30 legal entities, 150
bank accounts and 5 currencies. It calculates daily cash positions and forecasts 30-day liquidity
under BASE, DELAYED_RECEIPT and STRESS scenarios. It also detects five classes of data anomaly,
keeps every forecast version, and explains actual-versus-forecast variance down to source records.
Everything is deterministic (seed `20261022`), validated and reproducible.

The project is a plain Python package (pandas, NumPy, PyYAML, SQLite, pytest) with no web
framework, services or ML.

**Data: hybrid by default.** The treasury records (entities, accounts, ledger, receivables,
payables, forecasts, anomalies) are synthetic, because no public dataset of this kind exists.
**FX rates are real** (Kaggle, ODC-By, pinned version), and **customer payment delays are
calibrated** from real B2B invoices (Kaggle, Apache 2.0). Both come from small pinned files in
`data/external/`, so runs need no internet. `--data-mode synthetic` gives a fully synthetic run.
See [docs/provenance.md](docs/provenance.md), which also lists the four datasets evaluated and rejected.

## Quick start

```bash
pip install -r requirements.txt
pip install -e . --no-deps          # makes `python -m liqfc.cli` importable

python -m liqfc.cli all --scale starter --seed 20261022    # ~30-60 s (hybrid mode)
python -m liqfc.cli all --scale full --seed 20261022       # ~3-7 min, ~400 MB of output
python -m liqfc.cli all --scale starter --data-mode synthetic   # fully synthetic variant
python -m liqfc.cli validate --scale starter
python -m liqfc.cli reproduce --scale starter              # two full runs + checksum comparison
python scripts/reproduce.py · scripts/prepare_external_data.py --scale full                   # ~7 min
python -m liqfc.cli docs                                   # regenerate docs/data_dictionary.md
pytest                                                     # 73 tests, ~1 min
```

| Command | What it does (earlier stages run automatically) |
|---|---|
| `generate` | Build the clean baseline, validate it (it must pass), inject anomalies, then export the datasets, SQL and ground truth |
| `forecast` | Also calculate positions, detect anomalies, run forecasts, compare with actuals, raise alerts |
| `validate` | Also run structural and business validation on the baseline and injected data, plus the exception log and audit trail |
| `export` / `all` | Everything, plus evaluation metrics and the manifest (`--with-reproduce` adds the reproducibility test) |
| `reproduce` | Run `all` twice into temporary directories and compare row counts, SHA-256, bytes and validation summaries |

Options: `--scale starter|full`, `--seed N`, `--data-mode hybrid|synthetic`, `--output DIR`, `--config-dir DIR`.

Rebuilding the pinned external files (only needed when changing source versions): download them with
`kagglehub`, then run `python scripts/prepare_external_data.py --fx-csv <daily_forex_rates.csv> --ar-csv <Dataset.csv>`.
`make` targets with the same names exist in the [Makefile](Makefile).

## What is generated

| Entity (table) | Full | Notes |
|---|---|---|
| LegalEntity (`legal_entity`) | 30 | 7 countries, so all 5 functional currencies appear |
| BankAccount (`bank_account`) | 150 | 3–8 per entity; OPERATING / COLLECTION / DISBURSEMENT / PAYROLL / RESERVE |
| LedgerEntry (`ledger_entry`) | 100,000 | settlements, payroll, rent, tax, intercompany, one-offs, fees, derived interest, sundry |
| CashFlowForecast (`cash_flow_forecast`) | 20,000 (+20 injected duplicates) | from receivables and payables, including instalment plans |
| Receivable / Payable | ≈10.7k / ≈9.2k | delays by risk class (A/B calibrated on real invoices, incl. early payers), write-offs, partial payments, cancellations |
| CurrencyRate (`currency_rate`) | 508 (−10 removed in the anomaly set) | **real** rates, 127 business days × 4 currencies (synthetic mode: GBM with shocks) |
| LiquidityScenario / ScenarioAssumption | 3 / 15 | the only allowed scenario differences |
| ForecastRun (`forecast_run`) | 13 | 4 cutoffs × 3 scenarios + 1 re-run (supersedes) |

Supporting tables:
- **Inputs:** `actual_cash_flow`, `account_balance_statement`, `forecast_source_link` (N:M bridge), and status-event tables for receivables, payables and accounts.
- **Derived:** `daily_cash_position`, `consolidated_cash_position`, `forecast_run_account` and `forecast_run_line` (N:M bridges), `forecast_position`, `forecast_consolidated_position`, `forecast_comparison`, `variance_attribution`, `liquidity_alert`, `forecast_run_event`, `exception_log`, `audit_event`.

IDs follow the required conventions: `ENT-000001`, `BANKACC-ENT-000001-001`,
`LED-2026-00000001`, `CF-2026-000001` and `FCAST-2026-03-01-001`. They are assigned once, after a
stable sort, and are identical across CSV, JSON and SQL.

## Key rules (details in [docs/business_rules.md](docs/business_rules.md))

* **Money** is stored as integer minor units, and FX rates as integers × 10⁸. Rounding is half-even with exact integer arithmetic; floating point is never used for balances.
* **Balances:** closing = opening + credits − debits (by value date). Account balances roll up to entity and group levels in USD using effective-dated rates. A missing rate becomes a flagged STALE_FALLBACK and is never replaced by 1. Calculated balances reconcile to independently built bank statements.
* **Forecast cutoff:** a run uses only records whose `recorded_at` / `known_at` is before cutoff 00:00Z. Overdue items are re-dated by rule. Lines that would leak, mix currencies or double count are EXCLUDED with a reason.
* **Scenarios** change lines only through five documented assumptions, and every changed line carries its assumption IDs ([docs/scenarios.md](docs/scenarios.md)).
* **Variance** is attributed as TIMING, AMOUNT, UNFORECAST, ANOMALY, FX_MOVEMENT and RESIDUAL. Each driver has a source reference. Local components sum exactly; the reporting-currency residual is rounding only (≤ 6 minor units).
* **Anomalies** are injected after the baseline validates, labelled in a separate ground-truth file, and found by independent rules ([docs/anomaly_catalog.md](docs/anomaly_catalog.md)).
* **History** is append-only. A re-run creates a new version and supersedes the old one; lifecycle transitions are validated.

## Results (full scale, seed 20261022, hybrid mode, produced by `python -m liqfc.cli all --scale full`)

**Validation** (`output/full/reports/validation_summary.json`):

| Stage | Checks passed | Failed |
|---|---|---|
| Clean baseline inputs, before injection | 123 | 0 |
| Clean baseline + derived outputs (incl. SQLite FK load) | 203 | 0 |
| Injected dataset + derived outputs | 196 | 6: BUS-REC, BUS-STMT, BUS-FX, BUS-CCY, BUS-DUPSRC, BUS-CUT-ITEM, i.e. exactly the injected anomaly classes |

**Anomaly detection** vs ground truth: 85 labels, 85 detections, TP 85, FP 0, FN 0. Precision,
recall and F1 are 1.0 for every type (25 balance, 10 FX, 20 duplicate, 15 cutoff, 15 currency).
Detections on the clean baseline: 0.

**Data quality:**

| Metric | Baseline | Injected |
|---|---|---|
| PK uniqueness rate | 1.0 | 1.0 |
| FK pass rate | 1.0 | 1.0 |
| Reconciliation pass rate | 1.0 | 0.999079 (25 breaks) |
| Valid FX conversion coverage | 1.0 | 0.984088 (288 fallback conversions) |
| Validation pass rate | 1.0 | 0.970 |

**Forecast accuracy** (account level, USD, 54,000 matched rows, baseline): MAE 277,640,
RMSE 636,465, MAPE 7.6%. BASE MAPE is 6.1% with bias +82,639; the fully synthetic mode gave 7.1% and
+146,488, so calibrated delays make the forecast more accurate. Error grows with the horizon (MAPE
3.4% for days 1–7, 12.4% for days 22–30). STRESS is deliberately pessimistic (bias −254,835). Full
slices by scenario and horizon are in `output/full/reports/evaluation_report.md`.

**Liquidity:** STRESS lowers minimum group cash by 37–50M USD against BASE at every cutoff.
ENT-000001 breaches its policy minimum in the May and June STRESS runs (`liquidity_alert`).

**Reproducibility** (`output/<scale>/reports/reproducibility_report.md`): two independent
end-to-end runs into separate temporary directories produced byte-identical outputs. Full scale,
hybrid mode: 91 of 91 files match (SHA-256 and bytes), and the validation summaries are identical. The
fully synthetic mode was also verified (full 91/91, starter 92/92), and re-running it reproduces the
earlier synthetic metrics exactly.

## Project structure

```
config/        default.yaml (seed, external_data, calendar, FX, money, forecast, export) · scales.yaml · scenarios.yaml · anomalies.yaml
data/external/ fx_rates_kaggle_v1091.csv · ar_delay_calibration.json · SOURCES.json   (pinned, committed)
src/liqfc/
  cli.py · pipeline.py                     orchestration
  core/        config, rng, ids, schema, enums, money, calendar, dataset
  generators/  context, entities, accounts, fx_rates, receivables, payables, forecast_items,
               ledger, actual_cash_flows, balance_statements
  engine/      fx_conversion, balances, scenarios, forecast_run, variance, anomaly_detection,
               liquidity_alerts, evaluation
  anomalies/   injectors, ground_truth
  validation/  checks, foreign_keys, lifecycle, reconciliation, reproducibility, report
  audit/       events (append-only logs, run lifecycle), history (replay, append-only proof)
  exporters/   csv_json, sql_seed, data_dictionary, manifest
scripts/reproduce.py · scripts/prepare_external_data.py
docs/          architecture, data_dictionary (generated), erd, business_rules, scenarios, anomaly_catalog, provenance
tests/         ids, schema, foreign_keys, reconciliation, fx, scenarios, anomalies, forecast_cutoff,
               variance, determinism, lifecycle, external_data
output/<scale>/  dataset/ baseline/ ground_truth/ reports/ manifest.json   (git-ignored)
```

## Assumptions (all configurable)

* The data range is 2026-01-01 to 2026-06-30. Invoices are issued from 2025-12-10. There is one global calendar (Mon–Fri plus 3 holidays).
* The reporting currency is USD. Rates are quoted as USD per unit, with no cross rates. In hybrid mode the configured FX shocks and volatilities are not used (real rates already move); stress-scenario shocks still apply.
* Class C customers and write-off rates are not in the calibration data. They stay as documented stress assumptions (10% of invoices).
* Cutoffs are on the first of March to June with a 30-day horizon, so every horizon has full actuals.
* Opening balances are calibrated so the clean baseline never goes negative (LED-04). There is no cash pooling or sweeping between accounts.
* Run approval is simulated by a documented reviewer policy (RUN-01). There is no human workflow UI.
* The derived-table JSON export is skipped above 100,000 rows (the full `variance_attribution` table is CSV only) to limit output size. All input tables are always CSV and JSON.

## Limitations

* Sundry receipts and payments (~80% of ledger rows) exist so the ledger meets the exact 100,000-row target. They have no forecast items, so UNFORECAST is the largest variance driver and BASE carries a positive bias. A run-rate model for small flows would reduce this.
* Calibrated opening balances leave the group well above its policy minimum. Breaches therefore appear only at entity level under STRESS.
* Detectors are deterministic rules matched to the five specified anomaly classes. Perfect scores show the rules work on these classes, not that they generalise to unseen anomaly types.
* Byte-level reproducibility is verified on the same machine and environment (Python 3.11.9, pandas 2.3.3, NumPy 1.26.4, recorded in the manifest). Other library versions could change random draws.
* The Kaggle FX source does not name its upstream provider, so the rates are realistic market levels, not official fixings. Holidays still use one global calendar; a suitable per-country holiday dataset was found but deferred (see provenance).
* There is no dashboard. The spec made it optional after the backend passes; the outputs are designed to feed one.
