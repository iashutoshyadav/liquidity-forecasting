# Original challenge statement

**Source:** the statement as supplied by the project owner for "Code Challenge – Round 2,
Associate AI Forward Deployed Engineers · AI Forward Deployed Engineer". It is reproduced below
without editing, apart from Markdown formatting. Site navigation text was omitted.

**Deadline note:** the statement is titled "48 Hours Code Challenge". A later implementation brief
from the owner referred to a "72-Hour" challenge. The submission format and exact deadline aren't
specified beyond what appears below.

**Where each requirement is met:** see the traceability table in the [README](../README.md#16-requirements-traceability)
and the compliance matrix in [reports/ENGINEERING_AUDIT_REPORT.md](../reports/ENGINEERING_AUDIT_REPORT.md).

---

## Code Challenge – Round 2: 48 Hours Code Challenge

### Problem statement
Read it carefully. Your build should cover every point below.

### Business Context / Problem
Consolidate account balances and expected cash flows to forecast short-term liquidity under scenarios.

### Objective and Scope
Build a working solution for this end-to-end workflow: Consolidate account balances and expected cash flows to forecast short-term liquidity under scenarios. The solution should expose key decisions and exceptions, preserve traceability, and produce measurable outputs. Medium complexity means multiple related entities, conditional rules, at least one exception path and meaningful evaluation metrics.

### Primary Actors / Systems
Customer/account holder, operations analyst, risk/compliance reviewer and system administrator

### Business Workflow and Rules
1. Generate/reference master data first: LegalEntity, BankAccount, LedgerEntry, CashFlowForecast, Receivable.
2. Generate dependent transaction/event records using valid parent IDs.
3. Apply business logic: Create entities and bank accounts; generate opening balances and ledger entries; add scheduled receivables/payables with expected dates; create FX rates; calculate daily cash positions; define base, delayed-receipt and stress scenarios; store forecast versions and actual-versus-forecast comparisons.
4. Persist state changes as explicit records/events where relevant; do not overwrite history.
5. Generate outcomes and ground-truth labels from documented rules, not independent random assignment.

### Entities and Relationships
Core entities: LegalEntity, BankAccount, LedgerEntry, CashFlowForecast, Receivable, Payable, CurrencyRate, LiquidityScenario, ForecastRun. Define a primary key for every entity and foreign keys for every relationship. Document cardinality (1:1, 1:N, N:M), optional relationships, effective dates and lifecycle states. Use bridge tables for many-to-many relationships; retain event history for state changes; validate all joins. For deliberate anomaly cases, keep an issue_type/ground_truth label rather than silently corrupting unrelated rows.

### ID / Nomenclature Conventions
ENT-000001; BANKACC-<entity-id>-001; LED-2026-00000001; CF-2026-000001; FCAST-2026-01-01-001.

### Synthetic Data Generation Instructions (Granular)
- **Step 1** — Write a data dictionary for: LegalEntity, BankAccount, LedgerEntry, CashFlowForecast, Receivable, Payable, CurrencyRate, LiquidityScenario, ForecastRun. For each field specify type, required/optional, allowed values, units, uniqueness and validation rule.
- **Step 2** — Create parent/master tables first, then child and event tables. Apply this domain-specific generation logic: Create entities and bank accounts; generate opening balances and ledger entries; add scheduled receivables/payables with expected dates; create FX rates; calculate daily cash positions; define base, delayed-receipt and stress scenarios; store forecast versions and actual-versus-forecast comparisons.
- **Step 3** — Apply deterministic IDs and naming: ENT-000001; BANKACC-<entity-id>-001; LED-2026-00000001; CF-2026-000001; FCAST-2026-01-01-001. Keep counters zero-padded, IDs immutable, and referenced IDs exactly consistent across files.
- **Step 4** — Generate the baseline volume: Generate 30 entities, 150 bank accounts, 100,000 ledger entries and 20,000 forecast items across 5 currencies. Include delayed receivables, FX movement and large one-off payments. Use a fixed seed (e.g. 20261022) and parameterize scale so starter and full datasets use the same generator.
- **Step 5** — Generate valid baseline records first, then inject labelled edge cases: Opening balance plus ledger does not equal closing balance; FX rate missing; cash flow counted twice; forecast uses actual after cutoff; currency is mixed without conversion.
- **Step 6** — Calculate derived values (totals, balances, scores, statuses and dates) from source records rather than generating them independently.
- **Step 7** — Produce a separate ground_truth.csv/json for known anomalies, expected decisions or labels; never leak it into input features.
- **Step 8** — Run validation: primary-key uniqueness, foreign-key integrity, allowed enums, date ordering, arithmetic reconciliation, lifecycle transitions and duplicate checks.
- **Step 9** — Export CSV/JSON/SQL seed data plus README, schema/data dictionary, generator command, fixed seed, source manifest and validation summary.
- **Step 10** — Regenerate twice with the same seed and compare row counts/checksums to demonstrate reproducibility.

### Suggested Volume and Coverage
Generate 30 entities, 150 bank accounts, 100,000 ledger entries and 20,000 forecast items across 5 currencies. Include delayed receivables, FX movement and large one-off payments.

### Required Edge Cases / Negative Scenarios
Opening balance plus ledger does not equal closing balance; FX rate missing; cash flow counted twice; forecast uses actual after cutoff; currency is mixed without conversion.

### Validation Rules / Acceptance Criteria
Daily balances reconcile; scenarios differ only by documented assumptions; forecasts are reproducible; actual-versus-forecast variance is explainable.

### Evaluation Focus for Generated Data
Review: (a) realistic distributions and meaningful variation; (b) consistent IDs and valid joins; (c) dependency/lifecycle consistency across entities; (d) correct derived values and reconciliation; (e) normal, boundary, failure and exception coverage; (f) separate ground truth; (g) fixed-seed reproducibility, documentation and lawful provenance.

### Public Data Guidance / Lawful Use
External data is optional. If used, record source URL, dataset/version, retrieval date, license/terms, attribution, transformations and fields discarded. Use only data you are legally permitted to access and redistribute; do not bypass access controls or collect confidential/personal information. If licensing or suitability is unclear, use synthetic data and cite public reference data only for schema inspiration.
