from liqfc.core import money
from liqfc.core.config import load_settings
from liqfc.core.schema import GROUND_TRUTH_COLUMNS, INPUT_TABLES, SCHEMA


def test_nine_core_entities_present():
    entities = {t.entity for t in SCHEMA.values()}
    for core in ["LegalEntity", "BankAccount", "LedgerEntry", "CashFlowForecast", "Receivable", "Payable",
                 "CurrencyRate", "LiquidityScenario", "ForecastRun"]:
        assert core in entities


def test_every_table_has_pk_and_fks_resolve():
    for t in SCHEMA.values():
        assert t.pk and all(c in t.columns for c in t.pk)
        assert len(t.columns) == len(set(t.columns))
        for f in t.fields:
            if f.fk:
                table, col = f.fk.split(".")
                assert table in SCHEMA and col in SCHEMA[table].columns, f"{t.name}.{f.name}"


def test_ground_truth_not_in_input_schema():
    for name in INPUT_TABLES:
        cols = set(SCHEMA[name].columns)
        assert "issue_type" not in cols and "anomaly_id" not in cols
    assert "issue_type" in GROUND_TRUTH_COLUMNS


def test_config_loads_both_scales():
    s = load_settings("full")
    assert s.seed == 20261022
    assert s.scale["legal_entities"] == 30 and s.scale["bank_accounts"] == 150
    assert s.scale["ledger_entries"] == 100000 and s.scale["forecast_items"] == 20000
    assert set(s.minor_units) == {"USD", "EUR", "GBP", "INR", "JPY"}
    assert load_settings("starter").config_hash != s.config_hash
    assert load_settings("full", seed=1).config_hash != s.config_hash


def test_money_half_even_and_conversion():
    assert money.div_half_even(5, 2) == 2 and money.div_half_even(7, 2) == 4
    assert money.div_half_even(-5, 2) == -2 and money.div_half_even(-7, 2) == -4
    assert money.parse_rate("1.08") == 108_000_000
    assert money.format_rate(670_000) == "0.00670000"
    assert money.convert(1_000_000, 0, money.parse_rate("0.0067"), 2) == 670_000
    assert money.convert_inverse(670_000, 2, money.parse_rate("0.0067"), 0) == 1_000_000
    assert money.format_minor(123456, 2) == "1234.56" and money.format_minor(-5, 0) == "-5"
    assert money.shock_rate(100_000_000, -500) == 95_000_000
