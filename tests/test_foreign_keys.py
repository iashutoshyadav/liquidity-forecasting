from liqfc.core.schema import INPUT_TABLES
from liqfc.exporters.sql_seed import ddl, seed
from liqfc.validation.checks import structural_checks
from liqfc.validation.foreign_keys import fk_checks, sqlite_fk_check


def test_all_baseline_fks_valid(baseline, base_result):
    tables = {**baseline.tables, **base_result.derived}
    failed = [c.check_id for c in fk_checks(tables) if c.status == "FAIL"]
    assert failed == []


def test_structural_checks_pass_on_baseline(baseline):
    failed = [(c.check_id, c.failures[:2]) for c in structural_checks(baseline.tables) if c.status == "FAIL"]
    assert failed == []


def test_orphan_reference_is_caught(baseline):
    t = dict(baseline.tables)
    led = t["ledger_entry"].copy()
    led.loc[0, "account_id"] = "BANKACC-ENT-999999-001"
    t["ledger_entry"] = led
    res = {c.check_id: c for c in fk_checks(t)}
    assert res["STR-FK-ledger_entry-account_id"].status == "FAIL"
    assert res["STR-FK-ledger_entry-account_id"].failures[0][1] == "BANKACC-ENT-999999-001"


def test_polymorphic_source_fk(baseline):
    t = dict(baseline.tables)
    cf = t["cash_flow_forecast"].copy()
    cf.loc[0, "source_id"] = "RCV-2026-999999"
    cf.loc[0, "source_type"] = "RECEIVABLE"
    t["cash_flow_forecast"] = cf
    res = {c.check_id: c for c in fk_checks(t)}
    assert res["STR-FK-cash_flow_forecast-source_id"].status == "FAIL"


def test_sqlite_seed_loads_with_integrity(baseline):
    names = list(INPUT_TABLES)
    res = sqlite_fk_check(ddl(names), seed(baseline.tables, names), {n: len(baseline[n]) for n in names})
    assert res.status == "PASS", res.failures[:5]
