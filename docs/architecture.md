# Architecture

## Pipeline

```
config/*.yaml (seed 20261022, scale, calendar, FX, scenarios, anomalies, export, external_data)
data/external/ (pinned real FX rates + delay calibration, built by scripts/prepare_external_data.py)
        │
        ▼
core/        config · rng (named independent streams) · ids · schema (single source of truth)
             money (integer minor units, half-even) · calendar · dataset
        │
        ▼
generators/  entities → accounts (+status events) → fx_rates → [scenarios] → receivables →
             payables → forecast_items (CF ids) → ledger (cash pool, opening calibration, fees,
             sundry, derived interest; also returns economic cash events) → actual_cash_flows →
             status events → forecast-item revisions → bank model (bank lines + statements from
             the cash events, never from ledger rows)
        │  clean baseline
        ▼
validation/  structural + business + volume + SQLite FK check on the clean baseline  ── must pass
        │
        ▼
anomalies/   injectors (5 labelled edge cases)  ──────────────►  ground_truth (separate file)
        │  injected dataset (detector input)                          │
        ▼                                                            │
engine/      balances → daily/consolidated positions (FX effective-dated)   │
             bank_reconciliation (ledger ↔ bank lines, explained vs breaks)  │
             anomaly_detection (5 independent rules, no ground truth)       │
             forecast_run (revisions as of cutoff, guards, scenarios, sweeps)│
             approval (RUN-01 scoped holds)                                 │
             variance (step attribution with source references)            │
             liquidity_alerts · audit (run lifecycle, audit trail)          │
        │                                                                   │
        ▼                                                                   ▼
validation/  baseline + injected validation reports ── engine/evaluation (metrics vs ground truth)
        │
        ▼
exporters/   canonical CSV / JSON · SQL schema + seed · data dictionary · manifest (SHA-256)
validation/reproducibility.py  run twice in temp dirs → compare checksums
```

The pipeline is a plain Python package with no web framework, services or ML. Each stage is a
pure function of the configuration and the outputs of earlier stages.

## Module responsibilities

| Module | Responsibility |
|---|---|
| `core/config.py` | Merge YAML files, resolve the scale, validate, compute the config hash |
| `core/rng.py` | `stream(seed, name)`: one PCG64 generator per module; `stable_unit` hash selection |
| `core/ids.py` | ID formatters and regex patterns (ENT-, BANKACC-, LED-, CF-, FCAST-, …) |
| `core/schema.py` | Every table: PK, fields, types, enums, units, FKs, patterns, rules |
| `core/money.py` | Exact integer money, rate×10⁸ conversion, half-even rounding, FX shocks, haircuts |
| `core/calendar.py` | Business days, rolling, payment-run weekdays, month ends |
| `core/dataset.py` | Named DataFrames conformed to schema column order and PK sort |
| `generators/*` | Master data first, then dependent transactions; hidden profiles drive documented rules |
| `engine/fx_conversion.py` | Effective-dated rate lookup (EXACT / PRIOR_BUSINESS_DAY / STALE_FALLBACK / MISSING); never substitutes 1 |
| `engine/balances.py` | Account → entity → group daily positions; currency quarantine; reconciliation status |
| `engine/bank_reconciliation.py` | Ledger ↔ bank-line matching (MATCHED / TIMING_DIFFERENCE / BANK_ONLY / LEDGER_ONLY / UNMATCHED_*), explained difference per day |
| `engine/approval.py` | Forecast-run approval policy: holds only for stale FX or material recent breaks on the run's accounts |
| `engine/scenarios.py` | Scenario tables and the only allowed transformation of BASE |
| `engine/forecast_run.py` | Run plan, item versions as of cutoff (`items_as_of`), guards, scenario and sweep lines, projections, run metrics |
| `engine/variance.py` | Actual vs forecast, step attribution, reporting-currency decomposition |
| `engine/anomaly_detection.py` | 5 detectors; their rule functions also act as forecast guards |
| `engine/liquidity_alerts.py` | Policy breaches per run and scope |
| `engine/evaluation.py` | Forecast, data-quality, detection and liquidity metrics |
| `anomalies/*` | Injection with original/injected values; ground-truth store |
| `validation/*` | Structural, FK (incl. polymorphic + SQLite), lifecycle, business, reproducibility |
| `audit/*` | Append-only logs, run lifecycle state machine, history replay |
| `exporters/*` | Canonical CSV/JSON, SQL DDL/seed, data dictionary, manifest |
| `pipeline.py` / `cli.py` | Stage orchestration and the command line |

## Key design decisions

* **Money is stored as integer minor units.** Amounts are int64 in pandas and exact Python ints in conversions. FX rates are integers scaled by 10⁸. Every rounding is half-even. Floating point is used only for random draws, before quantisation, and for summary statistics. It is never used for balances or reconciliation.
* **The schema is the single source of truth.** One spec drives the data dictionary, SQL DDL, canonical exports and structural validation.
* **Ground truth is isolated.** Labels live in `anomalies/ground_truth.py` and `output/<scale>/ground_truth/`. The detector module imports nothing from `anomalies`, and a test enforces this.
* **Detection drives decisions.** Rule functions are shared by the detector and the forecast engine. Records flagged as leakage or mixed currency are REJECTED (excluded and logged), duplicates are AUTO_RESOLVED (later inclusion excluded), and balance breaks and missing FX rates are HELD_FOR_REVIEW. A run that touches a held item stays EXECUTED and is not APPROVED.
* **History is append-only.** All state changes are events. A re-run creates a new `FCAST-<cutoff>-<seq>` and moves the old run to SUPERSEDED; nothing is overwritten.
* **Hybrid data, offline and pinned.** Real FX rates and delay calibration come from versioned extracts committed in `data/external/`. Their checksums enter the config hash and the manifest, and `--data-mode synthetic` reproduces the fully synthetic dataset.
* **Versioned forecast items.** Item values come from an append-only revision table; runs read the version known before their cutoff, so later information can't rewrite a past forecast.
* **Independent bank side.** Bank lines are produced from economic events by bank rules (clearing delays, bank-only charges). Reconciliation must explain every difference, or report a break.
* **Determinism.** Named RNG streams, a fixed generation timestamp, stable mergesorts, IDs assigned after sorting, and canonical LF/UTF-8 serialisation.

## Output layout (`output/<scale>/`)

| Path | Content |
|---|---|
| `dataset/input/{csv,json}` | Published post-injection input tables (what detectors see) |
| `dataset/derived/{csv,json}` | Engine outputs on that dataset (JSON omitted for tables > `derived_json_max_rows`) |
| `dataset/sql/schema.sql`, `seed.sql` | SQLite DDL (PK/FK/CHECK) and INSERTs for the input tables |
| `baseline/input/csv`, `baseline/derived/csv` | Clean baseline and its derived outputs (comparison reference) |
| `ground_truth/ground_truth.{csv,json}` | Anomaly labels |
| `reports/validation_summary.json` | Validation of the baseline before injection, the baseline with derived outputs, and the injected dataset |
| `reports/evaluation_summary.json`, `evaluation_report.md` | Metrics |
| `reports/reproducibility_report.{json,md}` | Output of `reproduce` |
| `reports/data_dictionary.md` | Copy of the generated dictionary |
| `manifest.json` | Every file with rows, bytes and SHA-256; seed, scale, config hash, version, command |
