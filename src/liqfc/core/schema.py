"""Table and field specifications -- the single source of truth.

The data dictionary (docs), SQL DDL, canonical exports and structural validation are all
generated from these specs, so they cannot drift apart.

dtypes: str | int (signed integer; money = minor units) | date (YYYY-MM-DD) |
        ts (UTC timestamp YYYY-MM-DDTHH:MM:SSZ) | decimal (exact decimal string) | bool
"""
from __future__ import annotations

from dataclasses import dataclass, field

from liqfc.core import enums as E
from liqfc.core.ids import PATTERNS


@dataclass(frozen=True)
class Field:
    name: str
    dtype: str
    required: bool = True
    enum: tuple | None = None
    unit: str = ""
    unique: bool = False
    fk: str | None = None
    pattern: str | None = None
    desc: str = ""
    rule: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    entity: str
    layer: str
    pk: tuple
    fields: tuple
    desc: str
    unique_together: tuple = field(default_factory=tuple)

    @property
    def columns(self) -> list[str]:
        return [f.name for f in self.fields]

    def get(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)


def F(name, dtype, required=True, enum=None, unit="", unique=False, fk=None, pattern=None, desc="", rule=""):
    return Field(name, dtype, required, tuple(enum) if enum else None, unit, unique, fk, pattern, desc, rule)


MINOR = "minor units of `currency`"
RMINOR = "minor units of reporting currency (USD cents)"
P = PATTERNS

TABLES: list[Table] = [
    Table("legal_entity", "LegalEntity", "input", ("entity_id",), (
        F("entity_id", "str", unique=True, pattern=P["entity"], desc="Legal entity identifier"),
        F("entity_name", "str", unique=True, desc="Synthetic legal name"),
        F("country_code", "str", enum=E.COUNTRIES, desc="ISO-3166 alpha-2 country of incorporation"),
        F("functional_currency", "str", enum=E.CURRENCIES, desc="Functional currency", rule="derived from country_code"),
        F("parent_entity_id", "str", required=False, fk="legal_entity.entity_id", desc="Parent in group hierarchy (null for group parent)"),
        F("size_band", "str", enum=E.SIZE_BANDS, desc="Size band derived from planned monthly revenue"),
        F("min_liquidity_reporting_minor", "int", unit=RMINOR, desc="Treasury policy minimum cash", rule=">= 0; = liquidity_policy_months x monthly revenue"),
        F("status", "str", enum=E.ENTITY_STATUS, desc="Entity status"),
        F("effective_from", "date", desc="Date the entity became part of the group"),
        F("effective_to", "date", required=False, desc="Date the entity left the group", rule=">= effective_from"),
    ), "Group companies that own bank accounts and cash flows."),
    Table("bank_account", "BankAccount", "input", ("account_id",), (
        F("account_id", "str", unique=True, pattern=P["account"], desc="BANKACC-<entity_id>-<seq>"),
        F("entity_id", "str", fk="legal_entity.entity_id", desc="Owning entity (N:1)"),
        F("bank_name", "str", desc="Synthetic bank name"),
        F("account_number_masked", "str", pattern=r"^\*{4}\d{4}$", desc="Masked account number"),
        F("currency", "str", enum=E.CURRENCIES, desc="Account currency; all entries must be in this currency"),
        F("account_type", "str", enum=E.ACCOUNT_TYPES, desc="Treasury role of the account"),
        F("opened_date", "date", desc="Account opening date", rule="<= opening_balance_date"),
        F("opening_balance_date", "date", desc="Date of the opening balance (dataset start)"),
        F("opening_balance_minor", "int", unit=MINOR, desc="Balance at start of opening_balance_date", rule="calibrated so baseline stays above buffer"),
        F("annual_interest_bp", "int", unit="basis points p.a.", desc="Credit interest rate", rule=">= 0"),
        F("sweep_parent_account_id", "str", required=False, fk="bank_account.account_id",
          desc="Cash-pool header (the entity's OPERATING account, same currency); null if not pooled"),
        F("sweep_target_minor", "int", required=False, unit=MINOR,
          desc="Balance a pooled account is swept back to each week", rule="> 0; set iff sweep_parent_account_id is set"),
        F("status", "str", enum=E.ACCOUNT_STATUS, desc="Current status = last account_status_event"),
    ), "Bank accounts; opening balances are the anchor of every daily balance."),
    Table("account_status_event", "AccountStatusEvent", "input", ("event_id",), (
        F("event_id", "str", unique=True, pattern=P["account_event"]),
        F("account_id", "str", fk="bank_account.account_id"),
        F("from_status", "str", required=False, enum=E.ACCOUNT_STATUS, desc="null on creation"),
        F("to_status", "str", enum=E.ACCOUNT_STATUS),
        F("event_date", "date"),
        F("recorded_at", "ts"),
        F("reason", "str"),
    ), "Append-only lifecycle history of bank accounts."),
    Table("currency_rate", "CurrencyRate", "input", ("rate_id",), (
        F("rate_id", "str", unique=True, pattern=P["rate"], desc="FX-<date>-<base>-<quote>"),
        F("rate_date", "date", desc="Business day the rate is effective for"),
        F("base_currency", "str", enum=E.CURRENCIES, desc="Currency being priced"),
        F("quote_currency", "str", enum=E.CURRENCIES, desc="Always the reporting currency"),
        F("rate", "decimal", unit="quote per 1 base, 8 dp", desc="Mid rate", rule="> 0"),
        F("source", "str", enum=("SYNTHETIC_GBM", "EXTERNAL_KAGGLE_FOREX"), desc="Rate source (see data/external/SOURCES.json)"),
        F("published_at", "ts", desc="Publication timestamp (16:00Z on rate_date)"),
    ), "Daily business-day FX rates to the reporting currency.", (("rate_date", "base_currency", "quote_currency"),)),
    Table("liquidity_scenario", "LiquidityScenario", "input", ("scenario_id",), (
        F("scenario_id", "str", unique=True, pattern=P["scenario"]),
        F("scenario_code", "str", unique=True, enum=E.SCENARIO_CODES),
        F("scenario_name", "str"),
        F("description", "str"),
        F("version", "int", rule=">= 1"),
        F("status", "str", enum=E.SCENARIO_STATUS),
        F("effective_from", "date"),
        F("parameter_hash", "str", desc="sha256 of the scenario's parameters"),
    ), "Scenario definitions (BASE, DELAYED_RECEIPT, STRESS)."),
    Table("scenario_assumption", "ScenarioAssumption", "input", ("assumption_id",), (
        F("assumption_id", "str", unique=True, pattern=P["assumption"]),
        F("scenario_id", "str", fk="liquidity_scenario.scenario_id"),
        F("parameter", "str"),
        F("value", "str"),
        F("unit", "str"),
        F("description", "str"),
    ), "Documented parameters; the only allowed differences between scenarios (1:N)."),
    Table("receivable", "Receivable", "input", ("receivable_id",), (
        F("receivable_id", "str", unique=True, pattern=P["receivable"]),
        F("entity_id", "str", fk="legal_entity.entity_id", desc="Creditor entity"),
        F("account_id", "str", fk="bank_account.account_id", desc="Collection account", rule="account.entity_id = entity_id"),
        F("customer_id", "str", required=False, pattern=P["customer"], desc="External customer (null for intercompany)"),
        F("counterparty_entity_id", "str", required=False, fk="legal_entity.entity_id", desc="Debtor entity for intercompany"),
        F("is_intercompany", "bool"),
        F("risk_class", "str", enum=E.RISK_CLASSES, desc="Customer payment-behaviour class"),
        F("invoice_date", "date"),
        F("due_date", "date", rule=">= invoice_date"),
        F("payment_terms_days", "int", unit="days"),
        F("instalment_count", "int", enum=None, rule="1 or 2"),
        F("currency", "str", enum=E.CURRENCIES, rule="= account currency"),
        F("amount_minor", "int", unit=MINOR, rule="> 0"),
        F("status", "str", enum=E.RECEIVABLE_STATUS, desc="Status at dataset end = last status event"),
    ), "Customer and intercompany invoices (expected inflows)."),
    Table("receivable_status_event", "ReceivableStatusEvent", "input", ("event_id",), (
        F("event_id", "str", unique=True, pattern=P["receivable_event"]),
        F("receivable_id", "str", fk="receivable.receivable_id"),
        F("from_status", "str", required=False, enum=E.RECEIVABLE_STATUS),
        F("to_status", "str", enum=E.RECEIVABLE_STATUS),
        F("event_date", "date"),
        F("recorded_at", "ts"),
        F("reason", "str"),
        F("related_actual_id", "str", required=False, fk="actual_cash_flow.actual_id"),
    ), "Append-only receivable lifecycle history."),
    Table("payable", "Payable", "input", ("payable_id",), (
        F("payable_id", "str", unique=True, pattern=P["payable"]),
        F("entity_id", "str", fk="legal_entity.entity_id"),
        F("account_id", "str", fk="bank_account.account_id", rule="account.entity_id = entity_id"),
        F("supplier_id", "str", required=False, pattern=P["supplier"]),
        F("counterparty_entity_id", "str", required=False, fk="legal_entity.entity_id"),
        F("ic_receivable_id", "str", required=False, fk="receivable.receivable_id", desc="Mirror receivable for intercompany (1:1)"),
        F("category", "str", enum=E.PAYABLE_CATEGORIES),
        F("description", "str"),
        F("invoice_date", "date", desc="Date the obligation became known"),
        F("scheduled_date", "date", rule=">= invoice_date"),
        F("currency", "str", enum=E.CURRENCIES, rule="= account currency"),
        F("amount_minor", "int", unit=MINOR, rule="> 0 (estimate for payroll)"),
        F("status", "str", enum=E.PAYABLE_STATUS),
    ), "Supplier, payroll, rent, tax, intercompany and one-off obligations."),
    Table("payable_status_event", "PayableStatusEvent", "input", ("event_id",), (
        F("event_id", "str", unique=True, pattern=P["payable_event"]),
        F("payable_id", "str", fk="payable.payable_id"),
        F("from_status", "str", required=False, enum=E.PAYABLE_STATUS),
        F("to_status", "str", enum=E.PAYABLE_STATUS),
        F("event_date", "date"),
        F("recorded_at", "ts"),
        F("reason", "str"),
        F("related_actual_id", "str", required=False, fk="actual_cash_flow.actual_id"),
    ), "Append-only payable lifecycle history."),
    Table("ledger_entry", "LedgerEntry", "input", ("ledger_entry_id",), (
        F("ledger_entry_id", "str", unique=True, pattern=P["ledger"]),
        F("account_id", "str", fk="bank_account.account_id"),
        F("value_date", "date", desc="Date the cash moved (drives balances)"),
        F("posting_date", "date", rule=">= value_date"),
        F("recorded_at", "ts", desc="When the entry became visible to treasury (cutoff test)"),
        F("currency", "str", enum=E.CURRENCIES, rule="= account currency"),
        F("amount_minor", "int", unit=MINOR, desc="Signed: + credit / - debit", rule="!= 0"),
        F("entry_type", "str", enum=E.LEDGER_ENTRY_TYPES),
        F("source_type", "str", required=False, enum=E.SOURCE_TYPES),
        F("source_id", "str", required=False, desc="Receivable/Payable id (polymorphic FK on source_type)"),
        F("counterparty_ref", "str", required=False),
        F("description", "str"),
    ), "Bank-account cash ledger (actual movements)."),
    Table("actual_cash_flow", "ActualCashFlow", "input", ("actual_id",), (
        F("actual_id", "str", unique=True, pattern=P["actual"]),
        F("cf_id", "str", fk="cash_flow_forecast.cf_id", desc="Forecast item this actual settles"),
        F("source_type", "str", enum=E.SOURCE_TYPES),
        F("source_id", "str"),
        F("ledger_entry_id", "str", unique=True, fk="ledger_entry.ledger_entry_id", desc="1:1 with ledger entry"),
        F("account_id", "str", fk="bank_account.account_id"),
        F("currency", "str", enum=E.CURRENCIES),
        F("amount_minor", "int", unit=MINOR, rule="= ledger amount"),
        F("actual_date", "date", rule="= ledger value_date"),
        F("recorded_at", "ts", rule="= ledger recorded_at"),
    ), "Settlements of forecastable business events, linked to ledger entries."),
    Table("account_balance_statement", "AccountBalanceStatement", "input", ("statement_id",), (
        F("statement_id", "str", unique=True, pattern=P["statement"]),
        F("account_id", "str", fk="bank_account.account_id"),
        F("statement_date", "date"),
        F("currency", "str", enum=E.CURRENCIES),
        F("opening_balance_minor", "int", unit=MINOR),
        F("total_credits_minor", "int", unit=MINOR, rule=">= 0"),
        F("total_debits_minor", "int", unit=MINOR, rule=">= 0"),
        F("closing_balance_minor", "int", unit=MINOR, rule="= opening + credits - debits"),
        F("entry_count", "int", rule=">= 0"),
        F("source", "str", enum=("SYNTHETIC_BANK_FEED",)),
    ), "Bank-reported end-of-day balances, built only from bank_statement_line.", (("account_id", "statement_date"),)),
    Table("bank_statement_line", "BankStatementLine", "input", ("bank_line_id",), (
        F("bank_line_id", "str", unique=True, pattern=P["bank_line"]),
        F("account_id", "str", fk="bank_account.account_id"),
        F("booking_date", "date", desc="Date the bank booked the transaction (drives statement balances)"),
        F("value_date", "date", desc="Economic date of the underlying transaction", rule="<= booking_date"),
        F("currency", "str", enum=E.CURRENCIES),
        F("amount_minor", "int", unit=MINOR, desc="Signed: + credit / - debit", rule="!= 0"),
        F("bank_tx_code", "str", enum=E.BANK_TX_CODES),
        F("counterparty_ref", "str", required=False),
        F("description", "str"),
    ), "The bank's own transaction feed, generated from economic events by bank rules (clearing delays, bank-only charges); never read from the ledger."),
    Table("cash_flow_forecast", "CashFlowForecast", "input", ("cf_id",), (
        F("cf_id", "str", unique=True, pattern=P["cf"]),
        F("entity_id", "str", fk="legal_entity.entity_id"),
        F("account_id", "str", fk="bank_account.account_id"),
        F("currency", "str", enum=E.CURRENCIES, rule="= account currency"),
        F("amount_minor", "int", unit=MINOR, desc="Signed expected amount", rule="!= 0"),
        F("flow_category", "str", enum=E.FLOW_CATEGORIES),
        F("expected_date", "date"),
        F("known_at", "ts", desc="When the item became known (cutoff test)"),
        F("source_type", "str", enum=E.SOURCE_TYPES),
        F("source_id", "str"),
        F("instalment_no", "int", rule=">= 1"),
        F("basis_actual_id", "str", required=False, fk="actual_cash_flow.actual_id", desc="Actual used to estimate the amount", rule="basis recorded_at < known_at"),
        F("status", "str", enum=E.CF_STATUS),
    ), "Expected cash-flow items (20,000 at full scale). Value fields are the latest revision."),
    Table("cash_flow_forecast_revision", "CashFlowForecastRevision", "input", ("revision_id",), (
        F("revision_id", "str", unique=True, pattern=P["revision"]),
        F("cf_id", "str", fk="cash_flow_forecast.cf_id"),
        F("revision_no", "int", rule="1, 2, ... contiguous per item; 1 = the original item"),
        F("known_at", "ts", desc="When this version became known", rule="strictly increasing with revision_no; revision 1 = item known_at"),
        F("expected_date", "date"),
        F("amount_minor", "int", unit=MINOR, rule="!= 0"),
        F("currency", "str", enum=E.CURRENCIES),
        F("basis_actual_id", "str", required=False, fk="actual_cash_flow.actual_id", rule="basis recorded_at < known_at"),
        F("reason", "str", enum=E.REVISION_REASONS),
    ), "Append-only version history of forecast items; forecast runs use the latest version known before cutoff.",
          (("cf_id", "revision_no"),)),
    Table("forecast_source_link", "ForecastSourceLink", "input", ("link_id",), (
        F("link_id", "str", unique=True, pattern=P["link"]),
        F("cf_id", "str", fk="cash_flow_forecast.cf_id"),
        F("source_type", "str", enum=E.SOURCE_TYPES),
        F("source_id", "str"),
        F("allocation_bp", "int", unit="basis points", rule="1..10000; sum per source <= 10000"),
    ), "Bridge: forecast items <-> source business events (N:M)."),
    Table("daily_cash_position", "DailyCashPosition", "derived", ("account_id", "position_date"), (
        F("account_id", "str", fk="bank_account.account_id"),
        F("entity_id", "str", fk="legal_entity.entity_id"),
        F("position_date", "date"),
        F("currency", "str", enum=E.CURRENCIES),
        F("opening_balance_minor", "int", unit=MINOR),
        F("credits_minor", "int", unit=MINOR),
        F("debits_minor", "int", unit=MINOR),
        F("closing_balance_minor", "int", unit=MINOR, rule="= opening + credits - debits"),
        F("entry_count", "int"),
        F("fx_rate", "decimal", required=False),
        F("fx_rate_date", "date", required=False),
        F("fx_rate_status", "str", enum=E.FX_RATE_STATUS),
        F("closing_balance_reporting_minor", "int", required=False, unit=RMINOR),
        F("statement_closing_minor", "int", required=False, unit=MINOR),
        F("reconciliation_difference_minor", "int", required=False, unit=MINOR, rule="statement - calculated"),
        F("explained_difference_minor", "int", required=False, unit=MINOR,
          desc="Part of the difference explained by open reconciling items (timing, bank-only, ledger-only)"),
        F("reconciliation_status", "str", enum=E.RECON_STATUS),
    ), "Calculated end-of-day balance per account, converted to reporting currency."),
    Table("bank_reconciliation_item", "BankReconciliationItem", "derived", ("recon_item_id",), (
        F("recon_item_id", "str", unique=True, pattern=P["recon_item"]),
        F("account_id", "str", fk="bank_account.account_id"),
        F("ledger_entry_id", "str", required=False, fk="ledger_entry.ledger_entry_id"),
        F("bank_line_id", "str", required=False, fk="bank_statement_line.bank_line_id"),
        F("amount_minor", "int", unit=MINOR),
        F("ledger_date", "date", required=False),
        F("bank_date", "date", required=False),
        F("match_status", "str", enum=E.MATCH_STATUS),
        F("explanation", "str"),
    ), "One row per ledger entry and bank line: matched, timing difference, bank-only or ledger-only."),
    Table("consolidated_cash_position", "ConsolidatedCashPosition", "derived", ("level", "scope_id", "position_date"), (
        F("level", "str", enum=E.CONSOLIDATION_LEVELS),
        F("scope_id", "str", desc="entity_id or GROUP"),
        F("position_date", "date"),
        F("reporting_currency", "str", enum=E.CURRENCIES),
        F("closing_balance_reporting_minor", "int", unit=RMINOR),
        F("account_count", "int"),
        F("accounts_converted", "int"),
        F("accounts_with_fallback_rate", "int"),
        F("min_liquidity_reporting_minor", "int", unit=RMINOR),
        F("below_threshold", "bool"),
    ), "Entity and group positions in reporting currency (sum of converted accounts)."),
    Table("forecast_run", "ForecastRun", "derived", ("forecast_run_id",), (
        F("forecast_run_id", "str", unique=True, pattern=P["run"], desc="FCAST-<cutoff date>-<seq>"),
        F("scenario_id", "str", fk="liquidity_scenario.scenario_id"),
        F("cutoff_date", "date"),
        F("cutoff_ts", "ts", desc="Only information recorded strictly before this is used"),
        F("generated_at", "ts"),
        F("horizon_days", "int"),
        F("horizon_end_date", "date"),
        F("reporting_currency", "str", enum=E.CURRENCIES),
        F("input_hash", "str", desc="sha256 of canonical input tables"),
        F("config_hash", "str"),
        F("calculation_version", "str"),
        F("run_status", "str", enum=E.RUN_STATUS, desc="Latest forecast_run_event status"),
        F("supersedes_run_id", "str", required=False, fk="forecast_run.forecast_run_id"),
        F("rerun_reason", "str", required=False),
        F("included_line_count", "int"),
        F("excluded_line_count", "int"),
        F("total_expected_inflows_reporting_minor", "int", unit=RMINOR),
        F("total_expected_outflows_reporting_minor", "int", unit=RMINOR),
        F("opening_group_cash_reporting_minor", "int", unit=RMINOR),
        F("min_group_cash_reporting_minor", "int", unit=RMINOR),
        F("min_group_cash_date", "date"),
        F("group_threshold_reporting_minor", "int", unit=RMINOR),
        F("first_group_breach_date", "date", required=False),
        F("group_days_below_threshold", "int"),
        F("negative_account_days", "int"),
    ), "Immutable, versioned forecast executions."),
    Table("forecast_run_account", "ForecastRunAccount", "derived", ("forecast_run_id", "account_id"), (
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("account_id", "str", fk="bank_account.account_id"),
        F("currency", "str", enum=E.CURRENCIES),
        F("opening_balance_minor", "int", unit=MINOR, desc="Known balance at cutoff"),
        F("known_entry_count", "int"),
        F("fx_rate", "decimal", required=False),
        F("fx_rate_date", "date", required=False),
        F("fx_rate_status", "str", enum=E.FX_RATE_STATUS),
        F("fx_shock_bp", "int", unit="basis points"),
        F("fx_rate_applied", "decimal", required=False),
        F("opening_balance_reporting_minor", "int", required=False, unit=RMINOR),
    ), "Bridge: forecast runs <-> accounts with starting balance and FX basis (N:M)."),
    Table("forecast_run_line", "ForecastRunLine", "derived", ("line_id",), (
        F("line_id", "str", unique=True, pattern=P["run_line"]),
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("cf_id", "str", required=False, fk="cash_flow_forecast.cf_id"),
        F("revision_id", "str", required=False, fk="cash_flow_forecast_revision.revision_id",
          desc="Item version used (latest known before cutoff)"),
        F("account_id", "str", fk="bank_account.account_id"),
        F("source_type", "str", enum=E.LINE_SOURCE_TYPES),
        F("source_id", "str"),
        F("currency", "str", enum=E.CURRENCIES),
        F("base_date", "date"),
        F("base_amount_minor", "int", unit=MINOR),
        F("adjusted_date", "date"),
        F("adjusted_amount_minor", "int", unit=MINOR),
        F("settled_before_cutoff_minor", "int", unit=MINOR),
        F("assumption_ids", "str", required=False),
        F("basis_record_id", "str"),
        F("basis_recorded_at", "ts"),
        F("line_status", "str", enum=E.LINE_STATUS),
        F("exclusion_reason", "str", required=False, enum=E.EXCLUSION_REASONS),
    ), "Bridge: forecast runs <-> forecast items, with scenario-adjusted values (N:M)."),
    Table("forecast_position", "ForecastPosition", "derived", ("forecast_run_id", "account_id", "forecast_date"), (
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("account_id", "str", fk="bank_account.account_id"),
        F("forecast_date", "date"),
        F("horizon_day", "int"),
        F("currency", "str", enum=E.CURRENCIES),
        F("opening_minor", "int", unit=MINOR),
        F("inflows_minor", "int", unit=MINOR),
        F("outflows_minor", "int", unit=MINOR),
        F("closing_minor", "int", unit=MINOR),
        F("closing_reporting_minor", "int", required=False, unit=RMINOR),
    ), "Projected daily balances per account."),
    Table("forecast_consolidated_position", "ForecastConsolidatedPosition", "derived",
          ("forecast_run_id", "level", "scope_id", "forecast_date"), (
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("level", "str", enum=E.CONSOLIDATION_LEVELS),
        F("scope_id", "str"),
        F("forecast_date", "date"),
        F("horizon_day", "int"),
        F("expected_inflows_reporting_minor", "int", unit=RMINOR),
        F("expected_outflows_reporting_minor", "int", unit=RMINOR),
        F("closing_reporting_minor", "int", unit=RMINOR),
        F("min_liquidity_reporting_minor", "int", unit=RMINOR),
        F("below_threshold", "bool"),
    ), "Projected entity and group liquidity in reporting currency."),
    Table("forecast_comparison", "ForecastComparison", "derived", ("forecast_run_id", "account_id", "position_date"), (
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("scenario_code", "str", enum=E.SCENARIO_CODES),
        F("account_id", "str", fk="bank_account.account_id"),
        F("position_date", "date"),
        F("horizon_day", "int"),
        F("currency", "str", enum=E.CURRENCIES),
        F("forecast_closing_minor", "int", unit=MINOR),
        F("actual_closing_minor", "int", unit=MINOR),
        F("variance_minor", "int", unit=MINOR, rule="actual - forecast = timing+amount+unforecast+anomaly"),
        F("timing_minor", "int", unit=MINOR),
        F("amount_minor", "int", unit=MINOR),
        F("unforecast_minor", "int", unit=MINOR),
        F("anomaly_minor", "int", unit=MINOR),
        F("forecast_closing_reporting_minor", "int", required=False, unit=RMINOR),
        F("actual_closing_reporting_minor", "int", required=False, unit=RMINOR),
        F("variance_reporting_minor", "int", required=False, unit=RMINOR),
        F("timing_reporting_minor", "int", required=False, unit=RMINOR),
        F("amount_reporting_minor", "int", required=False, unit=RMINOR),
        F("unforecast_reporting_minor", "int", required=False, unit=RMINOR),
        F("anomaly_reporting_minor", "int", required=False, unit=RMINOR),
        F("fx_movement_reporting_minor", "int", required=False, unit=RMINOR),
        F("residual_reporting_minor", "int", required=False, unit=RMINOR, rule="|residual| <= tolerance"),
        F("actual_fx_rate_status", "str", enum=E.FX_RATE_STATUS),
    ), "Actual vs forecast per run/account/date with variance attribution."),
    Table("variance_attribution", "VarianceAttribution", "derived", ("attribution_id",), (
        F("attribution_id", "str", unique=True, pattern=P["attribution"]),
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("account_id", "str", fk="bank_account.account_id"),
        F("category", "str", enum=E.VARIANCE_CATEGORIES),
        F("effective_date", "date", desc="Component applies to all comparison dates >= this"),
        F("amount_minor", "int", unit=MINOR),
        F("source_type", "str", enum=E.ATTRIBUTION_SOURCE_TYPES),
        F("source_id", "str"),
        F("source_count", "int"),
        F("rule_id", "str"),
        F("explanation", "str"),
    ), "Source-referenced variance drivers (step contributions)."),
    Table("liquidity_alert", "LiquidityAlert", "derived", ("alert_id",), (
        F("alert_id", "str", unique=True, pattern=P["alert"]),
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("level", "str", enum=E.CONSOLIDATION_LEVELS),
        F("scope_id", "str"),
        F("threshold_reporting_minor", "int", unit=RMINOR),
        F("min_projected_reporting_minor", "int", unit=RMINOR),
        F("min_projected_date", "date"),
        F("first_breach_date", "date"),
        F("days_below_threshold", "int"),
        F("severity", "str", enum=E.ALERT_SEVERITIES),
    ), "Projected breaches of the minimum-liquidity policy."),
    Table("forecast_run_event", "ForecastRunEvent", "derived", ("event_id",), (
        F("event_id", "str", unique=True, pattern=P["run_event"]),
        F("forecast_run_id", "str", fk="forecast_run.forecast_run_id"),
        F("from_status", "str", required=False, enum=E.RUN_STATUS),
        F("to_status", "str", enum=E.RUN_STATUS),
        F("event_ts", "ts"),
        F("actor_role", "str", enum=E.ACTOR_ROLES),
        F("reason", "str"),
    ), "Append-only forecast-run lifecycle history."),
    Table("exception_log", "ExceptionLog", "derived", ("exception_id",), (
        F("exception_id", "str", unique=True, pattern=P["exception"]),
        F("rule_id", "str"),
        F("issue_type", "str", enum=E.ISSUE_TYPES),
        F("source", "str", enum=E.EXCEPTION_SOURCES),
        F("affected_table", "str"),
        F("affected_record_id", "str"),
        F("forecast_run_id", "str", required=False, fk="forecast_run.forecast_run_id"),
        F("severity", "str", enum=E.SEVERITIES),
        F("observed_value", "str"),
        F("expected_value", "str"),
        F("decision", "str", enum=E.DECISIONS),
        F("resolution_status", "str", enum=E.RESOLUTION_STATUS),
        F("detected_at", "ts"),
        F("details", "str"),
    ), "Exceptions raised by detection, validation and forecast guards."),
    Table("audit_event", "AuditEvent", "derived", ("audit_id",), (
        F("audit_id", "str", unique=True, pattern=P["audit"]),
        F("event_ts", "ts"),
        F("actor_role", "str", enum=E.ACTOR_ROLES),
        F("action", "str", enum=E.AUDIT_ACTIONS),
        F("object_type", "str"),
        F("object_id", "str"),
        F("details", "str"),
    ), "Append-only system audit trail."),
]

SCHEMA: dict[str, Table] = {t.name: t for t in TABLES}
INPUT_TABLES = [t.name for t in TABLES if t.layer == "input"]
DERIVED_TABLES = [t.name for t in TABLES if t.layer == "derived"]

GROUND_TRUTH_COLUMNS = [
    "anomaly_id", "issue_type", "affected_table", "affected_record_id", "affected_field",
    "original_value", "injected_value", "expected_detection_rule", "expected_decision", "injection_stage", "notes",
]

POLYMORPHIC_FKS = [
    ("ledger_entry", "source_type", "source_id"),
    ("actual_cash_flow", "source_type", "source_id"),
    ("cash_flow_forecast", "source_type", "source_id"),
    ("forecast_source_link", "source_type", "source_id"),
]
SOURCE_TARGETS = {"RECEIVABLE": ("receivable", "receivable_id"), "PAYABLE": ("payable", "payable_id")}

DATE_ORDER_RULES = [
    ("legal_entity", "effective_from", "effective_to", True),
    ("bank_account", "opened_date", "opening_balance_date", True),
    ("receivable", "invoice_date", "due_date", False),
    ("payable", "invoice_date", "scheduled_date", True),
    ("ledger_entry", "value_date", "posting_date", True),
    ("forecast_run", "cutoff_date", "horizon_end_date", False),
]
