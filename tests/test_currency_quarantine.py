"""Regression tests: a ledger entry in the wrong currency is quarantined and logged, not fatal (audit M1)."""
import pandas as pd
import pytest

from liqfc.pipeline import build_exceptions, run_system
from liqfc.validation.checks import FAILURE_COLUMNS
from liqfc.validation.reconciliation import derived_checks


@pytest.fixture(scope="module")
def mislabelled(baseline):
    ds = baseline.copy()
    led = ds["ledger_entry"].copy()
    usd = led["account_id"].map(ds["bank_account"].set_index("account_id")["currency"]) == "USD"
    j = led.index[usd & (led["value_date"] == pd.Timestamp("2026-02-10"))][0]
    led.at[j, "currency"] = "EUR"
    ds["ledger_entry"] = led
    return ds, led.loc[j]


@pytest.fixture(scope="module")
def mislabelled_result(mislabelled, settings):
    ds, _ = mislabelled
    return run_system(ds, settings)


def test_pipeline_completes_and_logs_ccy_exception(mislabelled, mislabelled_result, settings):
    _, entry = mislabelled
    det = mislabelled_result.detections
    hit = det[(det["affected_record_id"] == entry["ledger_entry_id"])]
    assert list(hit["rule_id"]) == ["CCY-001"] and hit["decision"].iloc[0] == "REJECTED"
    assert "quarantined" in hit["details"].iloc[0]
    exc = build_exceptions(mislabelled_result, pd.DataFrame(columns=FAILURE_COLUMNS), settings)
    logged = exc[exc["affected_record_id"] == entry["ledger_entry_id"]]
    assert len(logged) == 1 and logged["issue_type"].iloc[0] == "MIXED_CURRENCY"


def test_quarantined_entry_is_excluded_from_balances(mislabelled, mislabelled_result, base_result):
    _, entry = mislabelled
    a = base_result.derived["daily_cash_position"].set_index(["account_id", "position_date"])
    b = mislabelled_result.derived["daily_cash_position"].set_index(["account_id", "position_date"])
    diff = (b["closing_balance_minor"] - a["closing_balance_minor"])
    acc_diff = diff.xs(entry["account_id"], level="account_id")
    after = acc_diff.index >= entry["value_date"]
    assert (acc_diff[after] == -entry["amount_minor"]).all() and (acc_diff[~after] == 0).all()
    assert (diff.drop(entry["account_id"], level="account_id") == 0).all()


def test_statement_difference_is_a_reconciling_item_not_a_break(mislabelled, mislabelled_result):
    _, entry = mislabelled
    pos = mislabelled_result.derived["daily_cash_position"]
    p = pos[pos["account_id"] == entry["account_id"]]
    assert set(p.loc[p["position_date"] >= entry["value_date"], "reconciliation_status"]) == {"RECONCILING_ITEM"}
    assert (pos["reconciliation_status"] != "BREAK").all()
    assert not (mislabelled_result.detections["rule_id"] == "BAL-001").any()


def test_derived_validation_passes_with_quarantine(mislabelled, mislabelled_result, settings, calfx):
    ds, _ = mislabelled
    checks = {c.check_id: c for c in derived_checks(ds.tables, mislabelled_result.derived, settings, calfx[0])}
    for cid in ("BUS-CUTOFF-RUN", "BUS-POS", "BUS-VAR", "BUS-SCN"):
        assert checks[cid].status == "PASS", (cid, checks[cid].failures[:3])


def test_forecasts_and_variance_exclude_quarantined_entry(mislabelled, mislabelled_result, settings):
    ds, entry = mislabelled
    ra = mislabelled_result.derived["forecast_run_account"]
    runs = mislabelled_result.derived["forecast_run"].set_index("forecast_run_id")
    led = ds["ledger_entry"]
    ok = led[led["ledger_entry_id"] != entry["ledger_entry_id"]]
    for rid, g in ra.groupby("forecast_run_id"):
        cut = runs.at[rid, "cutoff_ts"]
        exp = ok[ok["recorded_at"] < cut].groupby("account_id")["amount_minor"].sum()
        exp = g["account_id"].map(exp).fillna(0) + g["account_id"].map(ds["bank_account"].set_index("account_id")["opening_balance_minor"])
        assert (exp.to_numpy() == g["opening_balance_minor"].to_numpy()).all()
    att = mislabelled_result.derived["variance_attribution"]
    assert not (att["source_id"] == entry["ledger_entry_id"]).any()
    comp = mislabelled_result.derived["forecast_comparison"]
    assert (comp["variance_minor"] == comp[["timing_minor", "amount_minor", "unforecast_minor", "anomaly_minor"]].sum(axis=1)).all()
