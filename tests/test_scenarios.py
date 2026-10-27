import hashlib
from decimal import Decimal

import pandas as pd

from liqfc.core import money


def _lines(result, run_id):
    return result.derived["forecast_run_line"].query("forecast_run_id == @run_id")


def _runs_by_cutoff(result):
    runs = result.derived["forecast_run"]
    runs = runs[runs["supersedes_run_id"].isna()]
    return {c: dict(zip(g["scenario_id"], g["forecast_run_id"])) for c, g in runs.groupby("cutoff_date")}


def _unit(seed, code, key):
    return int.from_bytes(hashlib.sha256(f"{seed}|{code}|{key}".encode()).digest()[:8], "big") / 2**64


def test_three_scenarios_per_cutoff(base_result, settings):
    for cut, m in _runs_by_cutoff(base_result).items():
        assert set(m) == {"SCN-BASE", "SCN-DELAYED_RECEIPT", "SCN-STRESS"}


def test_scenario_equals_documented_transformation(base_result, baseline, settings, calfx):
    cal, _ = calfx
    cat = baseline["cash_flow_forecast"].set_index("cf_id")["flow_category"]
    for cut, m in _runs_by_cutoff(base_result).items():
        base = _lines(base_result, m["SCN-BASE"]).dropna(subset=["cf_id"]).set_index("cf_id")
        for code in ["DELAYED_RECEIPT", "STRESS"]:
            p = settings.scenarios[code]
            prop = float(Decimal(p["receivable_delay"]["proportion"]))
            sl = _lines(base_result, m[p["scenario_id"]])
            items = sl[sl["cf_id"].notna()].set_index("cf_id")
            assert set(items.index) == set(base.index)
            for cid, b in base.iterrows():
                exp_d, exp_a = b["base_date"], int(b["base_amount_minor"])
                if cat[cid] == "CUSTOMER_RECEIPT":
                    if _unit(settings.seed, code, cid) < prop:
                        exp_d = pd.Timestamp(cal.roll_forward([exp_d + pd.Timedelta(days=p["receivable_delay"]["delay_days"])])[0])
                    exp_a = money.div_half_even(exp_a * (10000 - p["collection_haircut_bp"]), 10000)
                assert items.at[cid, "adjusted_date"] == exp_d
                assert int(items.at[cid, "adjusted_amount_minor"]) == exp_a
            extra = sl[sl["source_type"] == "SCENARIO"]
            assert len(extra) == (1 if p["one_off_payment"] else 0)


def test_unrelated_cash_flows_unchanged(base_result, baseline):
    cat = baseline["cash_flow_forecast"].set_index("cf_id")["flow_category"]
    for cut, m in _runs_by_cutoff(base_result).items():
        base = _lines(base_result, m["SCN-BASE"]).dropna(subset=["cf_id"]).set_index("cf_id")
        dl = _lines(base_result, m["SCN-DELAYED_RECEIPT"])
        dl = dl[dl["cf_id"].notna()].set_index("cf_id")
        other = [c for c in base.index if cat[c] != "CUSTOMER_RECEIPT"]
        assert (base.loc[other, ["adjusted_date", "adjusted_amount_minor"]].values ==
                dl.loc[other, ["adjusted_date", "adjusted_amount_minor"]].values).all()
        changed = dl.index[(dl["adjusted_date"] != base.loc[dl.index, "adjusted_date"])]
        assert all(cat[c] == "CUSTOMER_RECEIPT" for c in changed)
        assert dl.loc[changed, "assumption_ids"].str.contains("ASM-DELAYED_RECEIPT-01").all()


def test_fx_shock_only_in_stress(base_result, settings):
    ra = base_result.derived["forecast_run_account"]
    runs = base_result.derived["forecast_run"].set_index("forecast_run_id")["scenario_id"]
    for r in ra.itertuples(index=False):
        shock = (settings.scenarios["STRESS"]["fx_shock_bp"] or {}).get(r.currency, 0) if runs[r.forecast_run_id] == "SCN-STRESS" else 0
        assert r.fx_shock_bp == shock
        if r.fx_rate is not None and isinstance(r.fx_rate, str):
            assert money.parse_rate(r.fx_rate_applied) == money.shock_rate(money.parse_rate(r.fx_rate), shock)


def test_stress_is_more_severe_than_base(base_result):
    runs = base_result.derived["forecast_run"]
    runs = runs[runs["supersedes_run_id"].isna()]
    for cut, g in runs.groupby("cutoff_date"):
        mins = dict(zip(g["scenario_id"], g["min_group_cash_reporting_minor"]))
        assert mins["SCN-STRESS"] < mins["SCN-BASE"]
        assert mins["SCN-DELAYED_RECEIPT"] <= mins["SCN-BASE"]


def test_scenario_assumptions_documented(baseline):
    a = baseline["scenario_assumption"]
    assert a.groupby("scenario_id").size().eq(5).all()
    assert a["assumption_id"].is_unique
