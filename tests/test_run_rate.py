"""FC-12 run-rate estimate for recurring flows with no forecast item (payroll not yet booked, sundry, fees)."""
import pandas as pd

from liqfc.core import money
from liqfc.core.config import load_settings
from liqfc.engine.forecast_run import run_rate_lines

CUT = pd.Timestamp("2026-03-01")


def _rr(ds, settings, cal, cutoff=CUT):
    return run_rate_lines(ds, settings, cal, cutoff)


def test_run_rate_lines_in_every_run_and_same_across_scenarios(base_result):
    lines = base_result.derived["forecast_run_line"]
    runs = base_result.derived["forecast_run"]
    rr = lines[lines["source_type"] == "RUN_RATE"]
    assert set(rr["forecast_run_id"]) == set(runs["forecast_run_id"])
    assert (rr["line_status"] == "INCLUDED").all() and rr["cf_id"].isna().all()
    key = ["account_id", "source_id", "adjusted_date", "adjusted_amount_minor"]
    for _, g in runs.groupby("cutoff_date"):
        frames = [rr.loc[rr["forecast_run_id"] == rid, key].reset_index(drop=True) for rid in g["forecast_run_id"]]
        assert all(f.equals(frames[0]) for f in frames[1:])


def test_run_rate_ignores_everything_recorded_after_cutoff(baseline, settings, calfx):
    cal = calfx[0]
    before = _rr(baseline, settings, cal)
    ds = baseline.copy()
    led = ds["ledger_entry"].copy()
    late = led["recorded_at"] >= CUT
    led.loc[late, "amount_minor"] = led.loc[late, "amount_minor"] * 3
    ds["ledger_entry"] = led
    pd.testing.assert_frame_equal(_rr(ds, settings, cal), before)
    assert (pd.to_datetime(before["basis_recorded_at"]) < CUT).all()


def test_small_flow_rate_matches_hand_calculation(baseline, settings, calfx):
    cal = calfx[0]
    rr = settings.forecast["run_rate"]
    lines = _rr(baseline, settings, cal)
    sundry = lines[lines["flow_category"] == "SUNDRY"]
    acct = sundry["account_id"].iloc[0]
    led = baseline["ledger_entry"]
    start = CUT - pd.Timedelta(days=int(rr["lookback_days"]))
    hist = led[(led["account_id"] == acct) & led["entry_type"].isin(rr["entry_types"]) & (led["recorded_at"] < CUT)
               & (led["value_date"] >= start)]
    n = len(cal.business_days(start, CUT - pd.Timedelta(days=1)))
    mine = sundry[sundry["account_id"] == acct]
    assert set(mine["adjusted_amount_minor"]) == {money.div_half_even(int(hist["amount_minor"].sum()), n)}
    window = cal.business_days(CUT, CUT + pd.Timedelta(days=int(settings.forecast["horizon_days"]) - 1))
    assert list(mine["adjusted_date"]) == [pd.Timestamp(d) for d in window]


def test_payroll_estimate_is_last_actual_on_pay_day(baseline, settings, calfx):
    cal = calfx[0]
    pay = _rr(baseline, settings, cal)
    pay = pay[pay["flow_category"] == "PAYROLL"]
    assert len(pay) > 0
    pay_day = pd.Timestamp(cal.roll_backward([pd.Timestamp("2026-03-25")])[0])
    assert (pay["adjusted_date"] == pay_day).all()
    led = baseline["ledger_entry"]
    known = led[(led["entry_type"] == "PAYROLL") & (led["recorded_at"] < CUT)]
    last = known.sort_values(["account_id", "value_date", "ledger_entry_id"]).drop_duplicates("account_id", keep="last")
    assert dict(zip(pay["account_id"], pay["adjusted_amount_minor"])) == dict(zip(last["account_id"], last["amount_minor"]))


def test_payroll_already_booked_is_not_estimated_twice(baseline, settings, calfx):
    cal = calfx[0]
    cf = baseline["cash_flow_forecast"]
    item = cf[(cf["flow_category"] == "PAYROLL") & (cf["expected_date"].dt.month == 3)].iloc[0]
    assert f"RR-PAYROLL-{item['account_id']}-202603" in set(_rr(baseline, settings, cal)["source_id"])
    ds = baseline.copy()
    early = CUT - pd.Timedelta(days=1)
    c = cf.copy()
    c.loc[c["cf_id"] == item["cf_id"], "known_at"] = early
    rev = ds["cash_flow_forecast_revision"].copy()
    rev.loc[(rev["cf_id"] == item["cf_id"]) & (rev["revision_no"] == 1), "known_at"] = early
    ds["cash_flow_forecast"], ds["cash_flow_forecast_revision"] = c, rev
    assert f"RR-PAYROLL-{item['account_id']}-202603" not in set(_rr(ds, settings, cal)["source_id"])


def test_variance_nets_run_rate_into_unforecast(base_result, settings):
    att = base_result.derived["variance_attribution"]
    est = att[att["source_type"] == "RUN_RATE_ESTIMATE"]
    assert len(est) > 0 and (est["category"] == "UNFORECAST").all()
    lines = base_result.derived["forecast_run_line"]
    runs = base_result.derived["forecast_run"].set_index("forecast_run_id")
    rr = lines[lines["source_type"] == "RUN_RATE"]
    rr = rr[rr["adjusted_date"] <= rr["forecast_run_id"].map(runs["horizon_end_date"]).clip(upper=settings.end)]
    assert est.groupby("forecast_run_id")["amount_minor"].sum().equals(
        -rr.groupby("forecast_run_id")["adjusted_amount_minor"].sum().astype(est["amount_minor"].dtype))


def test_run_rate_can_be_switched_off(baseline, calfx):
    s = load_settings("starter")
    s.raw["forecast"]["run_rate"]["enabled"] = False
    assert run_rate_lines(baseline, s, calfx[0], CUT).empty
