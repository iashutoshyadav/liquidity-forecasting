"""Regression tests for the cash pool and sundry realism changes (audit H2)."""
import pandas as pd

from liqfc.engine.forecast_run import opening_balances, sweep_lines


def test_pool_structure(baseline):
    acc = baseline["bank_account"].set_index("account_id")
    pooled = acc[acc["sweep_parent_account_id"].notna()]
    assert len(pooled) > 0
    for aid, r in pooled.iterrows():
        h = acc.loc[r["sweep_parent_account_id"]]
        assert h["account_type"] == "OPERATING" and h["entity_id"] == r["entity_id"] and h["currency"] == r["currency"]
        assert r["account_type"] in ("COLLECTION", "DISBURSEMENT") and int(r["sweep_target_minor"]) > 0
        assert int(r["opening_balance_minor"]) == int(r["sweep_target_minor"])
    assert acc.loc[acc["sweep_parent_account_id"].isna(), "sweep_target_minor"].isna().all()


def test_volumes_exact_with_sweeps(baseline, settings, calfx):
    led = baseline["ledger_entry"]
    pooled = baseline["bank_account"]["sweep_parent_account_id"].notna().sum()
    weeks = len(calfx[0].weekly_sweep_dates(settings.start, settings.end))
    assert len(led) == settings.scale["ledger_entries"]
    assert led["entry_type"].str.startswith("INTERNAL_TRANSFER").sum() == 2 * pooled * weeks


def test_actual_sweeps_net_to_zero_and_reset_targets(baseline, base_result):
    acc = baseline["bank_account"]
    led = baseline["ledger_entry"]
    sw = led[led["entry_type"].str.startswith("INTERNAL_TRANSFER")]
    pool = acc.set_index("account_id")["sweep_parent_account_id"].dropna()
    key = sw["account_id"].map(lambda a: pool.get(a, a))
    assert (sw.groupby([key, sw["value_date"]])["amount_minor"].sum() == 0).all()
    assert (sw["posting_date"] == sw["value_date"]).all()
    pos = base_result.derived["daily_cash_position"]
    target = acc.set_index("account_id")["sweep_target_minor"].dropna()
    p = pos[pos["account_id"].isin(target.index) & pos["position_date"].isin(set(sw["value_date"]))]
    assert len(p) > 0 and (p["closing_balance_minor"].to_numpy() == p["account_id"].map(target).astype(int).to_numpy()).all()


def test_projected_sweeps_follow_the_same_rule(baseline, base_result):
    acc = baseline["bank_account"]
    target = acc.set_index("account_id")["sweep_target_minor"].dropna()
    lines = base_result.derived["forecast_run_line"]
    sl = lines[lines["source_type"] == "SWEEP"]
    assert len(sl) > 0 and (sl.groupby(["forecast_run_id", "source_id"])["adjusted_amount_minor"].sum() == 0).all()
    fp = base_result.derived["forecast_position"]
    keyed = sl[sl["account_id"].isin(target.index)][["forecast_run_id", "account_id", "adjusted_date"]]
    m = keyed.merge(fp, left_on=["forecast_run_id", "account_id", "adjusted_date"],
                    right_on=["forecast_run_id", "account_id", "forecast_date"])
    assert len(m) == len(keyed) and (m["closing_minor"].to_numpy() == m["account_id"].map(target).astype(int).to_numpy()).all()


def test_projected_sweeps_ignore_post_cutoff_records(baseline, settings, calfx, base_result):
    cal = calfx[0]
    cutoff = pd.Timestamp("2026-04-01")
    window_end = cutoff + pd.Timedelta(days=settings.forecast["horizon_days"] - 1)
    run = base_result.derived["forecast_run"]
    rid = run.loc[(run["cutoff_date"] == cutoff) & (run["scenario_id"] == "SCN-BASE"), "forecast_run_id"].iloc[0]
    lines = base_result.derived["forecast_run_line"]
    lines = lines[(lines["forecast_run_id"] == rid) & (lines["source_type"] != "SWEEP")]
    before = sweep_lines(baseline["bank_account"], opening_balances(baseline, cutoff), lines, cal, cutoff, window_end)
    ds = baseline.copy()
    led = ds["ledger_entry"]
    pooled = ds["bank_account"].loc[ds["bank_account"]["sweep_parent_account_id"].notna(), "account_id"].iloc[0]
    late = led[led["account_id"] == pooled].iloc[[0]].copy()
    late["ledger_entry_id"], late["value_date"] = "LED-2026-99999999", cutoff - pd.Timedelta(days=1)
    late["posting_date"], late["recorded_at"] = cutoff, cutoff + pd.Timedelta(hours=18)
    ds["ledger_entry"] = pd.concat([led, late], ignore_index=True)
    after = sweep_lines(ds["bank_account"], opening_balances(ds, cutoff), lines, cal, cutoff, window_end)
    pd.testing.assert_frame_equal(before, after)


def test_sweep_variance_nets_to_zero_per_pool(baseline, base_result):
    att = base_result.derived["variance_attribution"]
    pool = baseline["bank_account"].set_index("account_id")["sweep_parent_account_id"].dropna()
    sw = att[att["source_type"] == "CASH_POOL_SWEEP"]
    assert len(sw) > 0
    key = sw["account_id"].map(lambda a: pool.get(a, a))
    assert (sw.groupby([sw["forecast_run_id"], key])["amount_minor"].sum() == 0).all()


def test_sundry_flows_are_small_balanced_and_seasonal(baseline):
    led = baseline["ledger_entry"]
    sund = led[led["entry_type"].str.startswith("SUNDRY")]
    external = led[~led["entry_type"].str.startswith("INTERNAL_TRANSFER")]
    assert sund["amount_minor"].abs().sum() / external["amount_minor"].abs().sum() < 0.20
    assert abs(sund["amount_minor"].sum()) / sund["amount_minor"].abs().sum() < 0.10
    by_day = sund["value_date"].dt.dayofweek.value_counts()
    assert by_day[4] > by_day[2] and by_day[0] > by_day[2]


def test_every_pooled_account_sweeps_every_week_even_when_quiet(baseline, settings, calfx):
    led = baseline["ledger_entry"]
    pooled = set(baseline["bank_account"].loc[baseline["bank_account"]["sweep_parent_account_id"].notna(), "account_id"])
    dates = set(calfx[0].weekly_sweep_dates(settings.start, settings.end))
    fees = led[led["description"].str.startswith("Cash pool sweep fee")]
    outs = led[led["entry_type"].str.startswith("INTERNAL_TRANSFER") & led["account_id"].isin(pooled)]
    expected = {(a, d) for a in pooled for d in dates}
    assert set(zip(fees["account_id"], fees["value_date"])) == expected
    assert set(zip(outs["account_id"], outs["value_date"])) == expected
    assert (fees["amount_minor"] < 0).all() and (outs["amount_minor"] != 0).all()


def test_sweep_projection_for_pooled_account_without_lines(baseline, settings, calfx):
    cutoff = pd.Timestamp("2026-04-01")
    window_end = cutoff + pd.Timedelta(days=settings.forecast["horizon_days"] - 1)
    no_lines = pd.DataFrame(columns=["account_id", "adjusted_date", "adjusted_amount_minor", "line_status"])
    opening = opening_balances(baseline, cutoff)
    out = sweep_lines(baseline["bank_account"], opening, no_lines, calfx[0], cutoff, window_end)
    acc = baseline["bank_account"].set_index("account_id")
    pooled = acc[acc["sweep_parent_account_id"].notna()]
    first = calfx[0].weekly_sweep_dates(cutoff, window_end)[0]
    for aid, r in pooled.iterrows():
        x = int(opening.set_index("account_id").at[aid, "opening_balance_minor"]) - int(r["sweep_target_minor"])
        got = out[(out["account_id"] == aid) & (out["adjusted_date"] == first)]["adjusted_amount_minor"]
        assert (list(got) == [-x]) if x else got.empty
    assert len(out) == 2 * int((opening.set_index("account_id").loc[pooled.index, "opening_balance_minor"]
                                != pooled["sweep_target_minor"].astype(int)).sum())
