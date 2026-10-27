"""Regression tests: a balance break only holds the forecast runs it can affect (audit M5, policy RUN-01)."""
import pandas as pd

from liqfc.pipeline import build_audit, build_exceptions, run_system
from liqfc.validation.checks import FAILURE_COLUMNS

CUTOFF = pd.Timestamp("2026-04-01")


def _break(baseline, date, delta_major, account_type="OPERATING", currency="USD"):
    ds = baseline.copy()
    acc = ds["bank_account"]
    a = acc[(acc["account_type"] == account_type) & (acc["currency"] == currency)]["account_id"].iloc[0]
    st = ds["account_balance_statement"].copy()
    i = st.index[(st["account_id"] == a) & (st["statement_date"] == pd.Timestamp(date))][0]
    st.at[i, "closing_balance_minor"] = int(st.at[i, "closing_balance_minor"]) + delta_major * 100
    ds["account_balance_statement"] = st
    return ds, a


def _status(res):
    r = res.derived["forecast_run"]
    return dict(zip(r["forecast_run_id"], r["run_status"]))


def test_clean_baseline_approves_every_original_run(base_result):
    st = _status(base_result)
    assert {k: v for k, v in st.items() if v != "SUPERSEDED"} and all(v in ("APPROVED", "SUPERSEDED") for v in st.values())
    assert base_result.run_holds == {}


def test_recent_material_break_holds_only_runs_at_that_cutoff(baseline, settings):
    ds, account = _break(baseline, CUTOFF - pd.Timedelta(days=3), 50_000)
    res = run_system(ds, settings)
    st = _status(res)
    held = {k for k, v in st.items() if v == "EXECUTED"}
    assert held and all(k.startswith("FCAST-2026-04-01-") for k in held)
    assert all(v in ("APPROVED", "SUPERSEDED") for k, v in st.items() if not k.startswith("FCAST-2026-04-01-"))
    assert all(any(account in r and "statement break" in r for r in res.run_holds[k]) for k in held)


def test_old_break_does_not_hold_any_run(baseline, settings):
    ds, _ = _break(baseline, "2026-01-15", 50_000)
    res = run_system(ds, settings)
    assert all(v in ("APPROVED", "SUPERSEDED") for v in _status(res).values())
    assert (res.derived["daily_cash_position"]["reconciliation_status"] == "BREAK").any()


def test_immaterial_break_does_not_hold(baseline, settings):
    ds, _ = _break(baseline, CUTOFF - pd.Timedelta(days=2), 5)
    res = run_system(ds, settings)
    assert all(v in ("APPROVED", "SUPERSEDED") for v in _status(res).values())


def test_stale_fx_at_cutoff_still_holds(baseline, settings):
    ds = baseline.copy()
    r = ds["currency_rate"]
    last_bd = pd.Timestamp("2026-03-31")
    ds["currency_rate"] = r[~((r["rate_date"] == last_bd) & (r["base_currency"] == "EUR"))].reset_index(drop=True)
    res = run_system(ds, settings)
    held = {k for k, v in _status(res).items() if v == "EXECUTED"}
    assert held and all(k.startswith("FCAST-2026-04-01-") for k in held)
    assert all(any("STALE_FALLBACK FX rate" in r for r in res.run_holds[k]) for k in held)


def test_holds_are_recorded_in_the_audit_trail(baseline, settings):
    ds, account = _break(baseline, CUTOFF - pd.Timedelta(days=3), 50_000)
    res = run_system(ds, settings)
    exc = build_exceptions(res, pd.DataFrame(columns=FAILURE_COLUMNS), settings)
    audit = build_audit(res, exc, {"passed": 0, "failed": 0}, settings)
    held = audit[audit["action"] == "FORECAST_RUN_HELD"]
    assert set(held["object_id"]) == set(res.run_holds)
    assert held["details"].str.contains(account).all() and held["details"].str.contains("RUN-01").all()
