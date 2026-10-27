import pandas as pd
import pytest

from liqfc.audit.events import RunLifecycle
from liqfc.engine.forecast_run import candidate_lines, opening_balances, plan_runs


def test_opening_balance_uses_only_pre_cutoff_records(baseline, base_result):
    ra = base_result.derived["forecast_run_account"]
    runs = base_result.derived["forecast_run"].set_index("forecast_run_id")
    led = baseline["ledger_entry"]
    for rid, g in ra.groupby("forecast_run_id"):
        cut = runs.at[rid, "cutoff_ts"]
        known = led[led["recorded_at"] < cut].groupby("account_id")["amount_minor"].sum()
        exp = g["account_id"].map(known).fillna(0) + g["account_id"].map(
            baseline["bank_account"].set_index("account_id")["opening_balance_minor"])
        assert (exp.values == g["opening_balance_minor"].values).all()


def test_future_records_do_not_change_forecast(baseline, settings, calfx):
    cal, _ = calfx
    cutoff = pd.Timestamp("2026-04-01")
    before = candidate_lines(baseline, settings, cal, cutoff)
    ob_before = opening_balances(baseline, cutoff)
    ds = baseline.copy()
    led = ds["ledger_entry"]
    late = led.iloc[[0]].copy()
    late["ledger_entry_id"] = "LED-2026-99999999"
    late["value_date"] = pd.Timestamp("2026-03-31")
    late["posting_date"] = pd.Timestamp("2026-04-01")
    late["recorded_at"] = pd.Timestamp("2026-04-01 18:00")
    ds["ledger_entry"] = pd.concat([led, late], ignore_index=True)
    cf = ds["cash_flow_forecast"]
    new_item = cf.iloc[[0]].copy()
    new_item["cf_id"] = "CF-2026-999999"
    new_item["known_at"] = pd.Timestamp("2026-04-01 09:00")
    new_item["expected_date"] = pd.Timestamp("2026-04-10")
    ds["cash_flow_forecast"] = pd.concat([cf, new_item], ignore_index=True)
    after = candidate_lines(ds, settings, cal, cutoff)
    pd.testing.assert_frame_equal(before, after)
    pd.testing.assert_frame_equal(ob_before, opening_balances(ds, cutoff))


def test_included_lines_known_before_cutoff(base_result, baseline):
    lines = base_result.derived["forecast_run_line"]
    runs = base_result.derived["forecast_run"].set_index("forecast_run_id")
    inc = lines[(lines["line_status"] == "INCLUDED") & lines["cf_id"].notna()]
    known = inc["cf_id"].map(baseline["cash_flow_forecast"].set_index("cf_id")["known_at"])
    assert (known < inc["forecast_run_id"].map(runs["cutoff_ts"])).all()
    assert (inc["basis_recorded_at"] < inc["forecast_run_id"].map(runs["cutoff_ts"])).all()


def test_leaky_items_excluded_from_runs(injected, inj_result):
    _, gt = injected
    leaky = set(gt.loc[gt["issue_type"] == "CUTOFF_LEAKAGE", "affected_record_id"])
    lines = inj_result.derived["forecast_run_line"]
    used = lines[lines["cf_id"].isin(leaky)]
    assert len(used) > 0
    assert (used["line_status"] == "EXCLUDED").all()
    assert (used["exclusion_reason"] == "CUTOFF_LEAKAGE").all()


def test_rerun_creates_new_version_and_preserves_history(base_result, settings):
    runs = base_result.derived["forecast_run"].set_index("forecast_run_id")
    rerun = runs[runs["supersedes_run_id"].notna()]
    assert len(rerun) == len(settings.forecast["reruns"])
    for rid, r in rerun.iterrows():
        old = r["supersedes_run_id"]
        assert old in runs.index and old != rid
        assert runs.at[old, "run_status"] == "SUPERSEDED"
        assert runs.at[old, "input_hash"] == r["input_hash"]
        assert rid.startswith(f"FCAST-{r['cutoff_date']:%Y-%m-%d}-")
    ev = base_result.derived["forecast_run_event"]
    assert ev["event_id"].is_unique
    assert len(ev[ev["forecast_run_id"].isin(rerun["supersedes_run_id"])]) >= 3


def test_run_ids_follow_convention(settings):
    ids = [s.run_id for s in plan_runs(settings)]
    assert ids[0] == "FCAST-2026-03-01-001" and len(set(ids)) == len(ids)


def test_invalid_run_transition_rejected():
    lc = RunLifecycle(2026)
    lc.transition("FCAST-2026-01-01-001", "DRAFT", pd.Timestamp("2026-07-01"), "SYSTEM", "x")
    with pytest.raises(ValueError):
        lc.transition("FCAST-2026-01-01-001", "APPROVED", pd.Timestamp("2026-07-01"), "SYSTEM", "skip EXECUTED")
