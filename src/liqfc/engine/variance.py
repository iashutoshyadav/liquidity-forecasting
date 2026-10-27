"""Actual-versus-forecast comparison and source-referenced variance attribution.

For run R (cutoff C), account a, date d in the horizon (d <= data end):
  F(d) = B0 + sum(included lines with adjusted_date <= d)
  A(d) = calculated actual closing balance (value date)
  A(d) - B0 = sum of ledger entries recorded at/after C with value_date <= d   (entries recorded
  before C are already in B0). Each contribution is a step at its effective date:

VAR-TIM  TIMING      included item: -f at forecast date, +f at first actual date (if any)
                     actual of an item known before C but not forecast in the window: +a
VAR-AMT  AMOUNT      included item: +actual amounts, -f at first actual date; items that will
                     never settle (written off / cancelled / scenario-only): -f at forecast date
VAR-UNF  UNFORECAST  ledger entries with no forecast item, or items known after C (grouped per account/day)
VAR-ANO  ANOMALY     actuals of items flagged by the anomaly detector (excluded from the run)
Cash-pool sweeps (projected sweep lines and actual INTERNAL_TRANSFER entries) are AMOUNT with source
CASH_POOL_SWEEP; within a pool they net to zero.
Local identity: variance = timing + amount + unforecast + anomaly (exact integers).
Reporting: each local component converted at the run's applied rate; VAR-FX = A_rep(rate_d) -
A(rate_run); VAR-RES = reporting variance - sum(converted components) - FX (rounding only).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import money
from liqfc.core.enums import SWEEP_ENTRY_TYPES

EXPLAIN = {
    ("TIMING", "CASH_FLOW_FORECAST"): "Forecast item settled on a different date than forecast",
    ("AMOUNT", "CASH_FLOW_FORECAST"): "Settled amount differs from forecast amount / item will not settle",
    ("AMOUNT", "LEDGER_ENTRY"): "Actual settlement amount of a forecast item",
    ("AMOUNT", "SCENARIO_ASSUMPTION"): "Hypothetical scenario cash flow with no actual counterpart",
    ("TIMING", "LEDGER_ENTRY"): "Actual of an item known before cutoff but forecast outside this window",
    ("ANOMALY", "LEDGER_ENTRY"): "Actual of an item excluded because the anomaly detector flagged it",
    ("UNFORECAST", "LEDGER_GROUP"): "Ledger activity with no forecast item (fees, interest, sundry, new items)",
    ("AMOUNT", "CASH_POOL_SWEEP"): "Cash-pool sweep, projected vs actual (reallocates variance within the pool; nets to zero per pool)",
}
RULE = {"TIMING": "VAR-TIM", "AMOUNT": "VAR-AMT", "UNFORECAST": "VAR-UNF", "ANOMALY": "VAR-ANO"}


def attribute_run(run: pd.Series, lines: pd.DataFrame, ledger: pd.DataFrame, actuals: pd.DataFrame,
                  cf: pd.DataFrame, flagged: set[str], data_end: pd.Timestamp) -> pd.DataFrame:
    ts = pd.Timestamp(run["cutoff_ts"])
    wend = min(pd.Timestamp(run["horizon_end_date"]), data_end)
    inc = lines[(lines["forecast_run_id"] == run["forecast_run_id"]) & (lines["line_status"] == "INCLUDED")]
    never_status = cf.set_index("cf_id")["status"].isin(["WRITTEN_OFF", "CANCELLED"])
    known_at = cf.set_index("cf_id")["known_at"]

    new = ledger[(ledger["recorded_at"] >= ts) & (ledger["value_date"] <= wend)][
        ["ledger_entry_id", "account_id", "value_date", "amount_minor", "entry_type"]].copy()
    new["eff"] = new["value_date"].where(new["value_date"] >= ts, ts)
    new["cf_id"] = new["ledger_entry_id"].map(actuals.set_index("ledger_entry_id")["cf_id"])
    in_run = set(inc["cf_id"].dropna())

    rows = []
    first_actual = new[new["cf_id"].isin(in_run)].groupby("cf_id")["eff"].min()
    for r in inc.itertuples(index=False):
        f = int(r.adjusted_amount_minor)
        is_scn = r.source_type in ("SCENARIO", "SWEEP")
        src_t = {"SCENARIO": "SCENARIO_ASSUMPTION", "SWEEP": "CASH_POOL_SWEEP"}.get(r.source_type, "CASH_FLOW_FORECAST")
        src = r.source_id if is_scn else r.cf_id
        never = is_scn or bool(never_status.get(r.cf_id, False))
        fa = None if is_scn else first_actual.get(r.cf_id)
        if r.adjusted_date > wend and (fa is None or pd.isna(fa)):
            continue
        if never and (fa is None or pd.isna(fa)):
            rows.append((r.account_id, "AMOUNT", r.adjusted_date, -f, src_t, src, 1))
        else:
            rows.append((r.account_id, "TIMING", r.adjusted_date, -f, src_t, src, 1))
            if fa is not None and not pd.isna(fa):
                rows.append((r.account_id, "TIMING", fa, f, src_t, src, 1))
                rows.append((r.account_id, "AMOUNT", fa, -f, src_t, src, 1))
    det = pd.DataFrame(rows, columns=["account_id", "category", "effective_date", "amount_minor", "source_type",
                                      "source_id", "source_count"])

    linked = new["cf_id"].notna()
    cat = np.where(new["cf_id"].isin(in_run), "AMOUNT",
                   np.where(new["cf_id"].isin(flagged), "ANOMALY",
                            np.where(linked & (new["cf_id"].map(known_at) < ts), "TIMING", "UNFORECAST")))
    sweep = new["entry_type"].isin(SWEEP_ENTRY_TYPES).to_numpy()
    new["category"] = np.where(sweep, "AMOUNT", cat)
    indiv = new[new["category"] != "UNFORECAST"]
    det_e = pd.DataFrame({"account_id": indiv["account_id"], "category": indiv["category"], "effective_date": indiv["eff"],
                          "amount_minor": indiv["amount_minor"],
                          "source_type": np.where(indiv["entry_type"].isin(SWEEP_ENTRY_TYPES), "CASH_POOL_SWEEP", "LEDGER_ENTRY"),
                          "source_id": indiv["ledger_entry_id"], "source_count": 1})
    unf = new[new["category"] == "UNFORECAST"].groupby(["account_id", "eff"])["amount_minor"].agg(["sum", "size"]).reset_index()
    det_u = pd.DataFrame({"account_id": unf["account_id"], "category": "UNFORECAST", "effective_date": unf["eff"],
                          "amount_minor": unf["sum"], "source_type": "LEDGER_GROUP",
                          "source_id": [f"{a}|{d:%Y-%m-%d}" for a, d in zip(unf["account_id"], unf["eff"])],
                          "source_count": unf["size"]})
    out = pd.concat([det, det_e, det_u], ignore_index=True)
    out = out[out["effective_date"] <= wend]
    out["amount_minor"] = out["amount_minor"].astype(np.int64)
    out["forecast_run_id"] = run["forecast_run_id"]
    out["rule_id"] = out["category"].map(RULE)
    out["explanation"] = [EXPLAIN.get((c, s), c) for c, s in zip(out["category"], out["source_type"])]
    out = out.sort_values(["account_id", "effective_date", "category", "source_type", "source_id", "amount_minor"],
                          kind="mergesort").reset_index(drop=True)
    out["attribution_id"] = [f"{run['forecast_run_id']}-V{i:06d}" for i in range(1, len(out) + 1)]
    out["source_count"] = out["source_count"].astype(np.int64)
    return out


def compare_run(run: pd.Series, scenario_code: str, fpos: pd.DataFrame, run_acc: pd.DataFrame, actual_pos: pd.DataFrame,
                attribution: pd.DataFrame, settings) -> pd.DataFrame:
    rid = run["forecast_run_id"]
    mu = settings.minor_units
    rep = settings.reporting_currency
    dst = mu[rep]
    f = fpos[fpos["forecast_run_id"] == rid][["account_id", "forecast_date", "horizon_day", "currency", "closing_minor",
                                               "closing_reporting_minor"]].rename(columns={"forecast_date": "position_date"})
    a = actual_pos[["account_id", "position_date", "closing_balance_minor", "rate_e8", "fx_rate_status"]]
    c = f.merge(a, on=["account_id", "position_date"], how="inner")
    c = c.rename(columns={"closing_minor": "forecast_closing_minor", "closing_balance_minor": "actual_closing_minor",
                          "closing_reporting_minor": "forecast_closing_reporting_minor", "fx_rate_status": "actual_fx_rate_status"})
    c["variance_minor"] = c["actual_closing_minor"] - c["forecast_closing_minor"]

    piv = attribution.pivot_table(index=["account_id", "effective_date"], columns="category", values="amount_minor",
                                  aggfunc="sum", fill_value=0)
    for cat in ["TIMING", "AMOUNT", "UNFORECAST", "ANOMALY"]:
        if cat not in piv:
            piv[cat] = 0
    piv = piv[["TIMING", "AMOUNT", "UNFORECAST", "ANOMALY"]].astype(np.int64)
    days = sorted(c["position_date"].unique())
    grid = pd.MultiIndex.from_product([sorted(c["account_id"].unique()), days], names=["account_id", "effective_date"])
    allidx = piv.index.union(grid)
    cum = piv.reindex(allidx, fill_value=0).groupby(level=0).cumsum().reindex(grid).reset_index()
    cum = cum.rename(columns={"effective_date": "position_date", "TIMING": "timing_minor", "AMOUNT": "amount_minor",
                              "UNFORECAST": "unforecast_minor", "ANOMALY": "anomaly_minor"})
    c = c.merge(cum, on=["account_id", "position_date"], how="left")

    ra = c["account_id"].map(run_acc[run_acc["forecast_run_id"] == rid].set_index("account_id")["applied_e8"])
    out_cols = {k: [] for k in ["actual_closing_reporting_minor", "variance_reporting_minor", "timing_reporting_minor",
                                "amount_reporting_minor", "unforecast_reporting_minor", "anomaly_reporting_minor",
                                "fx_movement_reporting_minor", "residual_reporting_minor"]}
    for row, r_run in zip(c.itertuples(index=False), ra):
        ccy = row.currency
        if ccy == rep:
            r_act, r_fc = money.RATE_SCALE, money.RATE_SCALE
        else:
            r_act, r_fc = row.rate_e8, r_run
        if r_act is None or pd.isna(r_act) or r_fc is None or pd.isna(r_fc) or pd.isna(row.forecast_closing_reporting_minor):
            for k in out_cols:
                out_cols[k].append(None)
            continue
        su = mu[ccy]
        conv = lambda x, r: x if ccy == rep else money.convert(int(x), su, int(r), dst)
        a_rep = conv(row.actual_closing_minor, r_act)
        v_rep = a_rep - int(row.forecast_closing_reporting_minor)
        comps = [conv(getattr(row, k), r_fc) for k in ["timing_minor", "amount_minor", "unforecast_minor", "anomaly_minor"]]
        fxm = a_rep - conv(row.actual_closing_minor, r_fc)
        out_cols["actual_closing_reporting_minor"].append(a_rep)
        out_cols["variance_reporting_minor"].append(v_rep)
        for k, v in zip(["timing_reporting_minor", "amount_reporting_minor", "unforecast_reporting_minor",
                         "anomaly_reporting_minor"], comps):
            out_cols[k].append(v)
        out_cols["fx_movement_reporting_minor"].append(fxm)
        out_cols["residual_reporting_minor"].append(v_rep - sum(comps) - fxm)
    for k, v in out_cols.items():
        c[k] = pd.array(v, dtype="Int64")
    c["forecast_run_id"] = rid
    c["scenario_code"] = scenario_code
    return c
