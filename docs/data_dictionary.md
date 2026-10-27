# Data dictionary

Generated from `src/liqfc/core/schema.py` (`python -m liqfc.cli docs`). Do not edit by hand.

Types: `str`, `int` (signed integer; money columns are **minor units** of the stated currency), `date` (YYYY-MM-DD), `ts` (UTC, YYYY-MM-DDTHH:MM:SSZ), `decimal` (exact decimal string), `bool`.

## Input tables (generated source data; what detectors see)

### `legal_entity` — LegalEntity

Group companies that own bank accounts and cash flows.

Primary key: `entity_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `entity_id` | str | Y | `^ENT-\d{6}$` | | Y | |  | Legal entity identifier |
| `entity_name` | str | Y | |  | Y | |  | Synthetic legal name |
| `country_code` | str | Y | US, DE, FR, NL, GB, IN, JP | |  | |  | ISO-3166 alpha-2 country of incorporation |
| `functional_currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | | derived from country_code | Functional currency |
| `parent_entity_id` | str | N | |  | | legal_entity.entity_id | | Parent in group hierarchy (null for group parent) |
| `size_band` | str | Y | SMALL, MEDIUM, LARGE | |  | |  | Size band derived from planned monthly revenue |
| `min_liquidity_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | >= 0; = liquidity_policy_months x monthly revenue | Treasury policy minimum cash |
| `status` | str | Y | ACTIVE, DORMANT | |  | |  | Entity status |
| `effective_from` | date | Y | |  | |  | | Date the entity became part of the group |
| `effective_to` | date | N | |  | |  | >= effective_from | Date the entity left the group |

### `bank_account` — BankAccount

Bank accounts; opening balances are the anchor of every daily balance.

Primary key: `account_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `account_id` | str | Y | `^BANKACC-ENT-\d{6}-\d{3}$` | | Y | |  | BANKACC-<entity_id>-<seq> |
| `entity_id` | str | Y | |  | | legal_entity.entity_id | | Owning entity (N:1) |
| `bank_name` | str | Y | |  | |  | | Synthetic bank name |
| `account_number_masked` | str | Y | `^\*{4}\d{4}$` | |  | |  | Masked account number |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | Account currency; all entries must be in this currency |
| `account_type` | str | Y | OPERATING, COLLECTION, DISBURSEMENT, PAYROLL, RESERVE | |  | |  | Treasury role of the account |
| `opened_date` | date | Y | |  | |  | <= opening_balance_date | Account opening date |
| `opening_balance_date` | date | Y | |  | |  | | Date of the opening balance (dataset start) |
| `opening_balance_minor` | int | Y | | minor units of `currency` | |  | calibrated so baseline stays above buffer | Balance at start of opening_balance_date |
| `annual_interest_bp` | int | Y | | basis points p.a. | |  | >= 0 | Credit interest rate |
| `sweep_parent_account_id` | str | N | |  | | bank_account.account_id | | Cash-pool header (the entity's OPERATING account, same currency); null if not pooled |
| `sweep_target_minor` | int | N | | minor units of `currency` | |  | > 0; set iff sweep_parent_account_id is set | Balance a pooled account is swept back to each week |
| `status` | str | Y | ACTIVE, DORMANT, CLOSED | |  | |  | Current status = last account_status_event |

### `account_status_event` — AccountStatusEvent

Append-only lifecycle history of bank accounts.

Primary key: `event_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `event_id` | str | Y | `^ASE-\d{4}-\d{6}$` | | Y | |  | |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `from_status` | str | N | ACTIVE, DORMANT, CLOSED | |  | |  | null on creation |
| `to_status` | str | Y | ACTIVE, DORMANT, CLOSED | |  | |  | |
| `event_date` | date | Y | |  | |  | |  |
| `recorded_at` | ts | Y | |  | |  | |  |
| `reason` | str | Y | |  | |  | |  |

### `currency_rate` — CurrencyRate

Daily business-day FX rates to the reporting currency.

Primary key: `rate_id`; unique: (rate_date, base_currency, quote_currency)

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `rate_id` | str | Y | `^FX-\d{4}-\d{2}-\d{2}-[A-Z]{3}-[A-Z]{3}$` | | Y | |  | FX-<date>-<base>-<quote> |
| `rate_date` | date | Y | |  | |  | | Business day the rate is effective for |
| `base_currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | Currency being priced |
| `quote_currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | Always the reporting currency |
| `rate` | decimal | Y | | quote per 1 base, 8 dp | |  | > 0 | Mid rate |
| `source` | str | Y | SYNTHETIC_GBM, EXTERNAL_KAGGLE_FOREX | |  | |  | Rate source (see data/external/SOURCES.json) |
| `published_at` | ts | Y | |  | |  | | Publication timestamp (16:00Z on rate_date) |

### `liquidity_scenario` — LiquidityScenario

Scenario definitions (BASE, DELAYED_RECEIPT, STRESS).

Primary key: `scenario_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `scenario_id` | str | Y | `^SCN-[A-Z_]+$` | | Y | |  | |
| `scenario_code` | str | Y | BASE, DELAYED_RECEIPT, STRESS | | Y | |  | |
| `scenario_name` | str | Y | |  | |  | |  |
| `description` | str | Y | |  | |  | |  |
| `version` | int | Y | |  | |  | >= 1 | |
| `status` | str | Y | DRAFT, ACTIVE, RETIRED | |  | |  | |
| `effective_from` | date | Y | |  | |  | |  |
| `parameter_hash` | str | Y | |  | |  | | sha256 of the scenario's parameters |

### `scenario_assumption` — ScenarioAssumption

Documented parameters; the only allowed differences between scenarios (1:N).

Primary key: `assumption_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `assumption_id` | str | Y | `^ASM-[A-Z_]+-\d{2}$` | | Y | |  | |
| `scenario_id` | str | Y | |  | | liquidity_scenario.scenario_id | |  |
| `parameter` | str | Y | |  | |  | |  |
| `value` | str | Y | |  | |  | |  |
| `unit` | str | Y | |  | |  | |  |
| `description` | str | Y | |  | |  | |  |

### `receivable` — Receivable

Customer and intercompany invoices (expected inflows).

Primary key: `receivable_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `receivable_id` | str | Y | `^RCV-\d{4}-\d{6}$` | | Y | |  | |
| `entity_id` | str | Y | |  | | legal_entity.entity_id | | Creditor entity |
| `account_id` | str | Y | |  | | bank_account.account_id | account.entity_id = entity_id | Collection account |
| `customer_id` | str | N | `^CUST-\d{5}$` | |  | |  | External customer (null for intercompany) |
| `counterparty_entity_id` | str | N | |  | | legal_entity.entity_id | | Debtor entity for intercompany |
| `is_intercompany` | bool | Y | |  | |  | |  |
| `risk_class` | str | Y | A, B, C | |  | |  | Customer payment-behaviour class |
| `invoice_date` | date | Y | |  | |  | |  |
| `due_date` | date | Y | |  | |  | >= invoice_date | |
| `payment_terms_days` | int | Y | | days | |  | |  |
| `instalment_count` | int | Y | |  | |  | 1 or 2 | |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | | = account currency | |
| `amount_minor` | int | Y | | minor units of `currency` | |  | > 0 | |
| `status` | str | Y | OPEN, PARTIALLY_PAID, OVERDUE, PAID, WRITTEN_OFF | |  | |  | Status at dataset end = last status event |

### `receivable_status_event` — ReceivableStatusEvent

Append-only receivable lifecycle history.

Primary key: `event_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `event_id` | str | Y | `^RSE-\d{4}-\d{6}$` | | Y | |  | |
| `receivable_id` | str | Y | |  | | receivable.receivable_id | |  |
| `from_status` | str | N | OPEN, PARTIALLY_PAID, OVERDUE, PAID, WRITTEN_OFF | |  | |  | |
| `to_status` | str | Y | OPEN, PARTIALLY_PAID, OVERDUE, PAID, WRITTEN_OFF | |  | |  | |
| `event_date` | date | Y | |  | |  | |  |
| `recorded_at` | ts | Y | |  | |  | |  |
| `reason` | str | Y | |  | |  | |  |
| `related_actual_id` | str | N | |  | | actual_cash_flow.actual_id | |  |

### `payable` — Payable

Supplier, payroll, rent, tax, intercompany and one-off obligations.

Primary key: `payable_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `payable_id` | str | Y | `^PAY-\d{4}-\d{6}$` | | Y | |  | |
| `entity_id` | str | Y | |  | | legal_entity.entity_id | |  |
| `account_id` | str | Y | |  | | bank_account.account_id | account.entity_id = entity_id | |
| `supplier_id` | str | N | `^SUPP-\d{5}$` | |  | |  | |
| `counterparty_entity_id` | str | N | |  | | legal_entity.entity_id | |  |
| `ic_receivable_id` | str | N | |  | | receivable.receivable_id | | Mirror receivable for intercompany (1:1) |
| `category` | str | Y | SUPPLIER, PAYROLL, RENT, TAX, INTERCOMPANY, ONE_OFF | |  | |  | |
| `description` | str | Y | |  | |  | |  |
| `invoice_date` | date | Y | |  | |  | | Date the obligation became known |
| `scheduled_date` | date | Y | |  | |  | >= invoice_date | |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | | = account currency | |
| `amount_minor` | int | Y | | minor units of `currency` | |  | > 0 (estimate for payroll) | |
| `status` | str | Y | SCHEDULED, APPROVED, PAID, CANCELLED | |  | |  | |

### `payable_status_event` — PayableStatusEvent

Append-only payable lifecycle history.

Primary key: `event_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `event_id` | str | Y | `^PSE-\d{4}-\d{6}$` | | Y | |  | |
| `payable_id` | str | Y | |  | | payable.payable_id | |  |
| `from_status` | str | N | SCHEDULED, APPROVED, PAID, CANCELLED | |  | |  | |
| `to_status` | str | Y | SCHEDULED, APPROVED, PAID, CANCELLED | |  | |  | |
| `event_date` | date | Y | |  | |  | |  |
| `recorded_at` | ts | Y | |  | |  | |  |
| `reason` | str | Y | |  | |  | |  |
| `related_actual_id` | str | N | |  | | actual_cash_flow.actual_id | |  |

### `ledger_entry` — LedgerEntry

Bank-account cash ledger (actual movements).

Primary key: `ledger_entry_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `ledger_entry_id` | str | Y | `^LED-\d{4}-\d{8}$` | | Y | |  | |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `value_date` | date | Y | |  | |  | | Date the cash moved (drives balances) |
| `posting_date` | date | Y | |  | |  | >= value_date | |
| `recorded_at` | ts | Y | |  | |  | | When the entry became visible to treasury (cutoff test) |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | | = account currency | |
| `amount_minor` | int | Y | | minor units of `currency` | |  | != 0 | Signed: + credit / - debit |
| `entry_type` | str | Y | CUSTOMER_RECEIPT, INTERCOMPANY_RECEIPT, SUPPLIER_PAYMENT, PAYROLL, RENT, TAX, INTERCOMPANY_PAYMENT, ONE_OFF_PAYMENT, BANK_FEE, INTEREST, SUNDRY_RECEIPT, SUNDRY_PAYMENT, INTERNAL_TRANSFER_IN, INTERNAL_TRANSFER_OUT | |  | |  | |
| `source_type` | str | N | RECEIVABLE, PAYABLE | |  | |  | |
| `source_id` | str | N | |  | |  | | Receivable/Payable id (polymorphic FK on source_type) |
| `counterparty_ref` | str | N | |  | |  | |  |
| `description` | str | Y | |  | |  | |  |

### `actual_cash_flow` — ActualCashFlow

Settlements of forecastable business events, linked to ledger entries.

Primary key: `actual_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `actual_id` | str | Y | `^ACT-\d{4}-\d{6}$` | | Y | |  | |
| `cf_id` | str | Y | |  | | cash_flow_forecast.cf_id | | Forecast item this actual settles |
| `source_type` | str | Y | RECEIVABLE, PAYABLE | |  | |  | |
| `source_id` | str | Y | |  | |  | |  |
| `ledger_entry_id` | str | Y | |  | Y | ledger_entry.ledger_entry_id | | 1:1 with ledger entry |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `amount_minor` | int | Y | | minor units of `currency` | |  | = ledger amount | |
| `actual_date` | date | Y | |  | |  | = ledger value_date | |
| `recorded_at` | ts | Y | |  | |  | = ledger recorded_at | |

### `account_balance_statement` — AccountBalanceStatement

Bank-reported end-of-day balances, built only from bank_statement_line.

Primary key: `statement_id`; unique: (account_id, statement_date)

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `statement_id` | str | Y | `^STMT-\d{4}-\d{6}$` | | Y | |  | |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `statement_date` | date | Y | |  | |  | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `opening_balance_minor` | int | Y | | minor units of `currency` | |  | |  |
| `total_credits_minor` | int | Y | | minor units of `currency` | |  | >= 0 | |
| `total_debits_minor` | int | Y | | minor units of `currency` | |  | >= 0 | |
| `closing_balance_minor` | int | Y | | minor units of `currency` | |  | = opening + credits - debits | |
| `entry_count` | int | Y | |  | |  | >= 0 | |
| `source` | str | Y | SYNTHETIC_BANK_FEED | |  | |  | |

### `bank_statement_line` — BankStatementLine

The bank's own transaction feed, generated from economic events by bank rules (clearing delays, bank-only charges); never read from the ledger.

Primary key: `bank_line_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `bank_line_id` | str | Y | `^BSL-\d{4}-\d{8}$` | | Y | |  | |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `booking_date` | date | Y | |  | |  | | Date the bank booked the transaction (drives statement balances) |
| `value_date` | date | Y | |  | |  | <= booking_date | Economic date of the underlying transaction |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `amount_minor` | int | Y | | minor units of `currency` | |  | != 0 | Signed: + credit / - debit |
| `bank_tx_code` | str | Y | RECEIPT, PAYMENT, FEE, INTEREST, TRANSFER | |  | |  | |
| `counterparty_ref` | str | N | |  | |  | |  |
| `description` | str | Y | |  | |  | |  |

### `cash_flow_forecast` — CashFlowForecast

Expected cash-flow items (20,000 at full scale). Value fields are the latest revision.

Primary key: `cf_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `cf_id` | str | Y | `^CF-\d{4}-\d{6}$` | | Y | |  | |
| `entity_id` | str | Y | |  | | legal_entity.entity_id | |  |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | | = account currency | |
| `amount_minor` | int | Y | | minor units of `currency` | |  | != 0 | Signed expected amount |
| `flow_category` | str | Y | CUSTOMER_RECEIPT, INTERCOMPANY_RECEIPT, SUPPLIER_PAYMENT, PAYROLL, RENT, TAX, INTERCOMPANY_PAYMENT, ONE_OFF_PAYMENT | |  | |  | |
| `expected_date` | date | Y | |  | |  | |  |
| `known_at` | ts | Y | |  | |  | | When the item became known (cutoff test) |
| `source_type` | str | Y | RECEIVABLE, PAYABLE | |  | |  | |
| `source_id` | str | Y | |  | |  | |  |
| `instalment_no` | int | Y | |  | |  | >= 1 | |
| `basis_actual_id` | str | N | |  | | actual_cash_flow.actual_id | basis recorded_at < known_at | Actual used to estimate the amount |
| `status` | str | Y | OPEN, PARTIALLY_SETTLED, SETTLED, CANCELLED, WRITTEN_OFF | |  | |  | |

### `cash_flow_forecast_revision` — CashFlowForecastRevision

Append-only version history of forecast items; forecast runs use the latest version known before cutoff.

Primary key: `revision_id`; unique: (cf_id, revision_no)

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `revision_id` | str | Y | `^CFR-\d{4}-\d{6}$` | | Y | |  | |
| `cf_id` | str | Y | |  | | cash_flow_forecast.cf_id | |  |
| `revision_no` | int | Y | |  | |  | 1, 2, ... contiguous per item; 1 = the original item | |
| `known_at` | ts | Y | |  | |  | strictly increasing with revision_no; revision 1 = item known_at | When this version became known |
| `expected_date` | date | Y | |  | |  | |  |
| `amount_minor` | int | Y | | minor units of `currency` | |  | != 0 | |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `basis_actual_id` | str | N | |  | | actual_cash_flow.actual_id | basis recorded_at < known_at | |
| `reason` | str | Y | INITIAL, CUSTOMER_PROMISE, REESTIMATE | |  | |  | |

### `forecast_source_link` — ForecastSourceLink

Bridge: forecast items <-> source business events (N:M).

Primary key: `link_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `link_id` | str | Y | `^FSL-\d{4}-\d{6}$` | | Y | |  | |
| `cf_id` | str | Y | |  | | cash_flow_forecast.cf_id | |  |
| `source_type` | str | Y | RECEIVABLE, PAYABLE | |  | |  | |
| `source_id` | str | Y | |  | |  | |  |
| `allocation_bp` | int | Y | | basis points | |  | 1..10000; sum per source <= 10000 | |

## Derived tables (calculated by the engine)

### `daily_cash_position` — DailyCashPosition

Calculated end-of-day balance per account, converted to reporting currency.

Primary key: `account_id, position_date`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `entity_id` | str | Y | |  | | legal_entity.entity_id | |  |
| `position_date` | date | Y | |  | |  | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `opening_balance_minor` | int | Y | | minor units of `currency` | |  | |  |
| `credits_minor` | int | Y | | minor units of `currency` | |  | |  |
| `debits_minor` | int | Y | | minor units of `currency` | |  | |  |
| `closing_balance_minor` | int | Y | | minor units of `currency` | |  | = opening + credits - debits | |
| `entry_count` | int | Y | |  | |  | |  |
| `fx_rate` | decimal | N | |  | |  | |  |
| `fx_rate_date` | date | N | |  | |  | |  |
| `fx_rate_status` | str | Y | IDENTITY, EXACT, PRIOR_BUSINESS_DAY, STALE_FALLBACK, MISSING | |  | |  | |
| `closing_balance_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `statement_closing_minor` | int | N | | minor units of `currency` | |  | |  |
| `reconciliation_difference_minor` | int | N | | minor units of `currency` | |  | statement - calculated | |
| `explained_difference_minor` | int | N | | minor units of `currency` | |  | | Part of the difference explained by open reconciling items (timing, bank-only, ledger-only) |
| `reconciliation_status` | str | Y | RECONCILED, RECONCILING_ITEM, BREAK | |  | |  | |

### `bank_reconciliation_item` — BankReconciliationItem

One row per ledger entry and bank line: matched, timing difference, bank-only or ledger-only.

Primary key: `recon_item_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `recon_item_id` | str | Y | `^REC-\d{4}-\d{8}$` | | Y | |  | |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `ledger_entry_id` | str | N | |  | | ledger_entry.ledger_entry_id | |  |
| `bank_line_id` | str | N | |  | | bank_statement_line.bank_line_id | |  |
| `amount_minor` | int | Y | | minor units of `currency` | |  | |  |
| `ledger_date` | date | N | |  | |  | |  |
| `bank_date` | date | N | |  | |  | |  |
| `match_status` | str | Y | MATCHED, TIMING_DIFFERENCE, BANK_ONLY, LEDGER_ONLY, UNMATCHED_BANK, UNMATCHED_LEDGER | |  | |  | |
| `explanation` | str | Y | |  | |  | |  |

### `consolidated_cash_position` — ConsolidatedCashPosition

Entity and group positions in reporting currency (sum of converted accounts).

Primary key: `level, scope_id, position_date`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `level` | str | Y | ENTITY, GROUP | |  | |  | |
| `scope_id` | str | Y | |  | |  | | entity_id or GROUP |
| `position_date` | date | Y | |  | |  | |  |
| `reporting_currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `closing_balance_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `account_count` | int | Y | |  | |  | |  |
| `accounts_converted` | int | Y | |  | |  | |  |
| `accounts_with_fallback_rate` | int | Y | |  | |  | |  |
| `min_liquidity_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `below_threshold` | bool | Y | |  | |  | |  |

### `forecast_run` — ForecastRun

Immutable, versioned forecast executions.

Primary key: `forecast_run_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `forecast_run_id` | str | Y | `^FCAST-\d{4}-\d{2}-\d{2}-\d{3}$` | | Y | |  | FCAST-<cutoff date>-<seq> |
| `scenario_id` | str | Y | |  | | liquidity_scenario.scenario_id | |  |
| `cutoff_date` | date | Y | |  | |  | |  |
| `cutoff_ts` | ts | Y | |  | |  | | Only information recorded strictly before this is used |
| `generated_at` | ts | Y | |  | |  | |  |
| `horizon_days` | int | Y | |  | |  | |  |
| `horizon_end_date` | date | Y | |  | |  | |  |
| `reporting_currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `input_hash` | str | Y | |  | |  | | sha256 of canonical input tables |
| `config_hash` | str | Y | |  | |  | |  |
| `calculation_version` | str | Y | |  | |  | |  |
| `run_status` | str | Y | DRAFT, EXECUTED, APPROVED, REJECTED, SUPERSEDED | |  | |  | Latest forecast_run_event status |
| `supersedes_run_id` | str | N | |  | | forecast_run.forecast_run_id | |  |
| `rerun_reason` | str | N | |  | |  | |  |
| `included_line_count` | int | Y | |  | |  | |  |
| `excluded_line_count` | int | Y | |  | |  | |  |
| `total_expected_inflows_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `total_expected_outflows_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `opening_group_cash_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `min_group_cash_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `min_group_cash_date` | date | Y | |  | |  | |  |
| `group_threshold_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `first_group_breach_date` | date | N | |  | |  | |  |
| `group_days_below_threshold` | int | Y | |  | |  | |  |
| `negative_account_days` | int | Y | |  | |  | |  |

### `forecast_run_account` — ForecastRunAccount

Bridge: forecast runs <-> accounts with starting balance and FX basis (N:M).

Primary key: `forecast_run_id, account_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `opening_balance_minor` | int | Y | | minor units of `currency` | |  | | Known balance at cutoff |
| `known_entry_count` | int | Y | |  | |  | |  |
| `fx_rate` | decimal | N | |  | |  | |  |
| `fx_rate_date` | date | N | |  | |  | |  |
| `fx_rate_status` | str | Y | IDENTITY, EXACT, PRIOR_BUSINESS_DAY, STALE_FALLBACK, MISSING | |  | |  | |
| `fx_shock_bp` | int | Y | | basis points | |  | |  |
| `fx_rate_applied` | decimal | N | |  | |  | |  |
| `opening_balance_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |

### `forecast_run_line` — ForecastRunLine

Bridge: forecast runs <-> forecast items, with scenario-adjusted values (N:M).

Primary key: `line_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `line_id` | str | Y | `^FCAST-\d{4}-\d{2}-\d{2}-\d{3}-L\d{6}$` | | Y | |  | |
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `cf_id` | str | N | |  | | cash_flow_forecast.cf_id | |  |
| `revision_id` | str | N | |  | | cash_flow_forecast_revision.revision_id | | Item version used (latest known before cutoff) |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `source_type` | str | Y | RECEIVABLE, PAYABLE, SCENARIO, SWEEP | |  | |  | |
| `source_id` | str | Y | |  | |  | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `base_date` | date | Y | |  | |  | |  |
| `base_amount_minor` | int | Y | | minor units of `currency` | |  | |  |
| `adjusted_date` | date | Y | |  | |  | |  |
| `adjusted_amount_minor` | int | Y | | minor units of `currency` | |  | |  |
| `settled_before_cutoff_minor` | int | Y | | minor units of `currency` | |  | |  |
| `assumption_ids` | str | N | |  | |  | |  |
| `basis_record_id` | str | Y | |  | |  | |  |
| `basis_recorded_at` | ts | Y | |  | |  | |  |
| `line_status` | str | Y | INCLUDED, EXCLUDED, OUT_OF_HORIZON | |  | |  | |
| `exclusion_reason` | str | N | CUTOFF_LEAKAGE, MIXED_CURRENCY, DUPLICATE_CASH_FLOW | |  | |  | |

### `forecast_position` — ForecastPosition

Projected daily balances per account.

Primary key: `forecast_run_id, account_id, forecast_date`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `forecast_date` | date | Y | |  | |  | |  |
| `horizon_day` | int | Y | |  | |  | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `opening_minor` | int | Y | | minor units of `currency` | |  | |  |
| `inflows_minor` | int | Y | | minor units of `currency` | |  | |  |
| `outflows_minor` | int | Y | | minor units of `currency` | |  | |  |
| `closing_minor` | int | Y | | minor units of `currency` | |  | |  |
| `closing_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |

### `forecast_consolidated_position` — ForecastConsolidatedPosition

Projected entity and group liquidity in reporting currency.

Primary key: `forecast_run_id, level, scope_id, forecast_date`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `level` | str | Y | ENTITY, GROUP | |  | |  | |
| `scope_id` | str | Y | |  | |  | |  |
| `forecast_date` | date | Y | |  | |  | |  |
| `horizon_day` | int | Y | |  | |  | |  |
| `expected_inflows_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `expected_outflows_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `closing_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `min_liquidity_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `below_threshold` | bool | Y | |  | |  | |  |

### `forecast_comparison` — ForecastComparison

Actual vs forecast per run/account/date with variance attribution.

Primary key: `forecast_run_id, account_id, position_date`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `scenario_code` | str | Y | BASE, DELAYED_RECEIPT, STRESS | |  | |  | |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `position_date` | date | Y | |  | |  | |  |
| `horizon_day` | int | Y | |  | |  | |  |
| `currency` | str | Y | USD, EUR, GBP, INR, JPY | |  | |  | |
| `forecast_closing_minor` | int | Y | | minor units of `currency` | |  | |  |
| `actual_closing_minor` | int | Y | | minor units of `currency` | |  | |  |
| `variance_minor` | int | Y | | minor units of `currency` | |  | actual - forecast = timing+amount+unforecast+anomaly | |
| `timing_minor` | int | Y | | minor units of `currency` | |  | |  |
| `amount_minor` | int | Y | | minor units of `currency` | |  | |  |
| `unforecast_minor` | int | Y | | minor units of `currency` | |  | |  |
| `anomaly_minor` | int | Y | | minor units of `currency` | |  | |  |
| `forecast_closing_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `actual_closing_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `variance_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `timing_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `amount_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `unforecast_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `anomaly_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `fx_movement_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |  |
| `residual_reporting_minor` | int | N | | minor units of reporting currency (USD cents) | |  | |residual| <= tolerance | |
| `actual_fx_rate_status` | str | Y | IDENTITY, EXACT, PRIOR_BUSINESS_DAY, STALE_FALLBACK, MISSING | |  | |  | |

### `variance_attribution` — VarianceAttribution

Source-referenced variance drivers (step contributions).

Primary key: `attribution_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `attribution_id` | str | Y | `^FCAST-\d{4}-\d{2}-\d{2}-\d{3}-V\d{6}$` | | Y | |  | |
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `account_id` | str | Y | |  | | bank_account.account_id | |  |
| `category` | str | Y | TIMING, AMOUNT, UNFORECAST, ANOMALY | |  | |  | |
| `effective_date` | date | Y | |  | |  | | Component applies to all comparison dates >= this |
| `amount_minor` | int | Y | | minor units of `currency` | |  | |  |
| `source_type` | str | Y | CASH_FLOW_FORECAST, LEDGER_ENTRY, LEDGER_GROUP, SCENARIO_ASSUMPTION, CASH_POOL_SWEEP | |  | |  | |
| `source_id` | str | Y | |  | |  | |  |
| `source_count` | int | Y | |  | |  | |  |
| `rule_id` | str | Y | |  | |  | |  |
| `explanation` | str | Y | |  | |  | |  |

### `liquidity_alert` — LiquidityAlert

Projected breaches of the minimum-liquidity policy.

Primary key: `alert_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `alert_id` | str | Y | `^ALR-\d{4}-\d{6}$` | | Y | |  | |
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `level` | str | Y | ENTITY, GROUP | |  | |  | |
| `scope_id` | str | Y | |  | |  | |  |
| `threshold_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `min_projected_reporting_minor` | int | Y | | minor units of reporting currency (USD cents) | |  | |  |
| `min_projected_date` | date | Y | |  | |  | |  |
| `first_breach_date` | date | Y | |  | |  | |  |
| `days_below_threshold` | int | Y | |  | |  | |  |
| `severity` | str | Y | WARNING, CRITICAL | |  | |  | |

### `forecast_run_event` — ForecastRunEvent

Append-only forecast-run lifecycle history.

Primary key: `event_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `event_id` | str | Y | `^FRE-\d{4}-\d{6}$` | | Y | |  | |
| `forecast_run_id` | str | Y | |  | | forecast_run.forecast_run_id | |  |
| `from_status` | str | N | DRAFT, EXECUTED, APPROVED, REJECTED, SUPERSEDED | |  | |  | |
| `to_status` | str | Y | DRAFT, EXECUTED, APPROVED, REJECTED, SUPERSEDED | |  | |  | |
| `event_ts` | ts | Y | |  | |  | |  |
| `actor_role` | str | Y | SYSTEM, OPERATIONS_ANALYST, RISK_REVIEWER, SYSTEM_ADMIN, ACCOUNT_HOLDER | |  | |  | |
| `reason` | str | Y | |  | |  | |  |

### `exception_log` — ExceptionLog

Exceptions raised by detection, validation and forecast guards.

Primary key: `exception_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `exception_id` | str | Y | `^EXC-\d{4}-\d{6}$` | | Y | |  | |
| `rule_id` | str | Y | |  | |  | |  |
| `issue_type` | str | Y | BALANCE_MISMATCH, FX_RATE_MISSING, DUPLICATE_CASH_FLOW, CUTOFF_LEAKAGE, MIXED_CURRENCY, PK_VIOLATION, FK_VIOLATION, FORMAT_VIOLATION, ENUM_VIOLATION, REQUIRED_MISSING, DATE_ORDER_VIOLATION, LIFECYCLE_VIOLATION, BUSINESS_RULE_VIOLATION | |  | |  | |
| `source` | str | Y | DETECTION, VALIDATION, FORECAST_GUARD | |  | |  | |
| `affected_table` | str | Y | |  | |  | |  |
| `affected_record_id` | str | Y | |  | |  | |  |
| `forecast_run_id` | str | N | |  | | forecast_run.forecast_run_id | |  |
| `severity` | str | Y | LOW, MEDIUM, HIGH, CRITICAL | |  | |  | |
| `observed_value` | str | Y | |  | |  | |  |
| `expected_value` | str | Y | |  | |  | |  |
| `decision` | str | Y | AUTO_RESOLVED, HELD_FOR_REVIEW, REJECTED | |  | |  | |
| `resolution_status` | str | Y | OPEN, RESOLVED | |  | |  | |
| `detected_at` | ts | Y | |  | |  | |  |
| `details` | str | Y | |  | |  | |  |

### `audit_event` — AuditEvent

Append-only system audit trail.

Primary key: `audit_id`

| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |
|---|---|---|---|---|---|---|---|---|
| `audit_id` | str | Y | `^AUD-\d{4}-\d{6}$` | | Y | |  | |
| `event_ts` | ts | Y | |  | |  | |  |
| `actor_role` | str | Y | SYSTEM, OPERATIONS_ANALYST, RISK_REVIEWER, SYSTEM_ADMIN, ACCOUNT_HOLDER | |  | |  | |
| `action` | str | Y | FORECAST_RUN_CREATED, FORECAST_RUN_STATUS_CHANGED, EXCEPTION_RAISED, VALIDATION_COMPLETED, LIQUIDITY_ALERT_RAISED, FORECAST_RUN_HELD | |  | |  | |
| `object_type` | str | Y | |  | |  | |  |
| `object_id` | str | Y | |  | |  | |  |
| `details` | str | Y | |  | |  | |  |

## Ground truth (separate file, never joined into detector inputs)

`ground_truth/ground_truth.csv|json` columns: `anomaly_id`, `issue_type`, `affected_table`, `affected_record_id`, `affected_field`, `original_value`, `injected_value`, `expected_detection_rule`, `expected_decision`, `injection_stage`, `notes`
