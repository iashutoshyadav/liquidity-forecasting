"""Evaluation metrics computed from actual outputs (never hard-coded).

* Forecast accuracy uses account-level reporting-currency closing balances on matching
  (run, account, date) rows only. error = forecast - actual (bias > 0 = over-forecast).
  Rows without an actual or without a usable FX rate are excluded and counted.
  Percentage error only where |actual| >= pct_error_min_abs_actual_reporting_minor.
* Detection metrics compare detector output to ground truth on (issue_type, record id).
  Zero denominators yield null (precision with no detections, recall with no labels).
* Statistics are computed in float64 on exact integer inputs and rounded for reporting;
  financial reconciliation itself never uses floating point.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core.enums import ANOMALY_TYPES


def _ratio(n, d):
    return None if d == 0 else round(n / d, 6)


def _acc_stats(err: pd.Series, actual: pd.Series, min_abs: int) -> dict:
    if err.empty:
        return {"rows": 0, "mae": None, "rmse": None, "bias": None, "mape_pct": None, "mape_rows": 0}
    e = err.astype("float64") / 100.0
    a = actual.astype("float64")
    mask = a.abs() >= min_abs
    mape = (err[mask].abs().astype("float64") / a[mask].abs()).mean() * 100 if mask.any() else None
    return {"rows": int(len(err)), "mae": round(float(e.abs().mean()), 2), "rmse": round(float(np.sqrt((e**2).mean())), 2),
            "bias": round(float(e.mean()), 2), "mape_pct": None if mape is None else round(float(mape), 4),
            "mape_rows": int(mask.sum())}


def forecast_metrics(comparison: pd.DataFrame, runs: pd.DataFrame, settings) -> dict:
    min_abs = int(settings.raw["evaluation"]["pct_error_min_abs_actual_reporting_minor"])
    buckets = settings.raw["evaluation"]["horizon_buckets"]
    total = len(comparison)
    c = comparison[comparison["variance_reporting_minor"].notna()].copy()
    c = c[c["forecast_run_id"].isin(runs.loc[runs["run_status"] != "SUPERSEDED", "forecast_run_id"])]
    c["err"] = -c["variance_reporting_minor"].astype("int64")
    c["act"] = c["actual_closing_reporting_minor"].astype("int64")
    c["bucket"] = None
    for lo, hi in buckets:
        c.loc[(c["horizon_day"] >= lo) & (c["horizon_day"] <= hi), "bucket"] = f"D{lo:02d}-{hi:02d}"
    out = {"unit": settings.reporting_currency + " (major units)", "comparison_rows": total,
           "rows_excluded_missing_actual_or_fx": int(total - comparison["variance_reporting_minor"].notna().sum()),
           "rows_excluded_superseded_runs": int(comparison["variance_reporting_minor"].notna().sum() - len(c)),
           "account_level": {"overall": _acc_stats(c["err"], c["act"], min_abs), "by_scenario": {}, "by_horizon": {},
                             "by_scenario_horizon": {}}}
    al = out["account_level"]
    for k, g in c.groupby("scenario_code"):
        al["by_scenario"][k] = _acc_stats(g["err"], g["act"], min_abs)
    for k, g in c.groupby("bucket"):
        al["by_horizon"][k] = _acc_stats(g["err"], g["act"], min_abs)
    for (s, b), g in c.groupby(["scenario_code", "bucket"]):
        al["by_scenario_horizon"][f"{s}|{b}"] = _acc_stats(g["err"], g["act"], min_abs)
    grp = c.groupby(["forecast_run_id", "scenario_code", "position_date"])[["err", "act"]].sum().reset_index()
    out["group_level"] = {k: _acc_stats(g["err"], g["act"], min_abs) for k, g in grp.groupby("scenario_code")}

    comp_cols = ["timing_reporting_minor", "amount_reporting_minor", "unforecast_reporting_minor", "anomaly_reporting_minor",
                 "fx_movement_reporting_minor", "residual_reporting_minor"]
    last = c[c["horizon_day"] == c.groupby("forecast_run_id")["horizon_day"].transform("max")]
    expl = {}
    for k, g in last.groupby("scenario_code"):
        tot_abs = float(g[comp_cols].abs().sum().sum())
        expl[k] = {
            "total_variance": round(float(g["variance_reporting_minor"].sum()) / 100, 2),
            "components": {col.replace("_reporting_minor", ""): round(float(g[col].sum()) / 100, 2) for col in comp_cols},
            "abs_share": {col.replace("_reporting_minor", ""): (round(float(g[col].abs().sum()) / tot_abs, 6) if tot_abs else None)
                          for col in comp_cols},
            "max_abs_residual_minor": int(g["residual_reporting_minor"].abs().max()),
            "components_reconcile": bool((g["variance_reporting_minor"] == g[comp_cols].sum(axis=1)).all()),
        }
    out["variance_attribution_at_horizon_end"] = expl
    out["max_abs_residual_minor_all_rows"] = int(c["residual_reporting_minor"].abs().max()) if len(c) else None
    return out


def detection_metrics(detections: pd.DataFrame, ground_truth: pd.DataFrame) -> dict:
    d = detections[detections["issue_type"].isin(ANOMALY_TYPES)][["issue_type", "affected_record_id"]].drop_duplicates()
    g = ground_truth[["issue_type", "affected_record_id"]].drop_duplicates()
    m = d.merge(g, how="outer", indicator=True)
    res = {}
    for t in ANOMALY_TYPES + ("ALL",):
        mm = m if t == "ALL" else m[m["issue_type"] == t]
        tp = int((mm["_merge"] == "both").sum())
        fp = int((mm["_merge"] == "left_only").sum())
        fn = int((mm["_merge"] == "right_only").sum())
        p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
        f1 = None if p is None or r is None or (p + r) == 0 else round(2 * p * r / (p + r), 6)
        res[t] = {"labels": tp + fn, "detections": tp + fp, "tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}
    return res


def data_quality_metrics(summary: dict, positions: pd.DataFrame) -> dict:
    checks = summary["checks"]

    def rate(prefix):
        sel = [c for c in checks if c["check_id"].startswith(prefix)]
        n = sum(c["records_checked"] for c in sel)
        f = sum(c["failures"] for c in sel)
        return _ratio(n - f, n)

    rec = positions["reconciliation_status"].eq("RECONCILED")
    non_id = positions[positions["fx_rate_status"] != "IDENTITY"]
    valid = non_id["fx_rate_status"].isin(["EXACT", "PRIOR_BUSINESS_DAY"])
    return {"pk_uniqueness_rate": rate("STR-PK-"), "fk_pass_rate": rate("STR-FK-"),
            "reconciliation_pass_rate": _ratio(int(rec.sum()), len(rec)),
            "valid_fx_conversion_coverage": _ratio(int(valid.sum()), len(non_id)),
            "fx_conversions_with_fallback": int(non_id["fx_rate_status"].eq("STALE_FALLBACK").sum()),
            "validation_pass_rate": summary["pass_rate"]}


def liquidity_metrics(runs: pd.DataFrame, alerts: pd.DataFrame, scenarios: pd.DataFrame) -> dict:
    code = scenarios.set_index("scenario_id")["scenario_code"]
    r = runs[runs["run_status"] != "SUPERSEDED"].assign(code=runs["scenario_id"].map(code))
    per_run = {}
    for x in r.itertuples(index=False):
        per_run[x.forecast_run_id] = {
            "scenario": x.code, "cutoff": f"{x.cutoff_date:%Y-%m-%d}",
            "opening_group_cash": round(x.opening_group_cash_reporting_minor / 100, 2),
            "min_projected_group_cash": round(x.min_group_cash_reporting_minor / 100, 2),
            "min_projected_date": f"{x.min_group_cash_date:%Y-%m-%d}",
            "group_threshold": round(x.group_threshold_reporting_minor / 100, 2),
            "first_group_breach_date": None if pd.isna(x.first_group_breach_date) else f"{x.first_group_breach_date:%Y-%m-%d}",
            "group_days_below_threshold": int(x.group_days_below_threshold),
            "negative_account_days": int(x.negative_account_days),
            "entity_alerts": int((alerts["forecast_run_id"] == x.forecast_run_id).sum()),
            "first_entity_breach_date": (None if not (alerts["forecast_run_id"] == x.forecast_run_id).any() else
                                         f"{alerts.loc[alerts['forecast_run_id'] == x.forecast_run_id, 'first_breach_date'].min():%Y-%m-%d}"),
        }
    diff = {}
    for cut, g in r.groupby("cutoff_date"):
        b = g.loc[g["code"] == "BASE", "min_group_cash_reporting_minor"]
        s = g.loc[g["code"] == "STRESS", "min_group_cash_reporting_minor"]
        dl = g.loc[g["code"] == "DELAYED_RECEIPT", "min_group_cash_reporting_minor"]
        if len(b) and len(s):
            diff[f"{cut:%Y-%m-%d}"] = {"base_min": round(int(b.iloc[0]) / 100, 2), "stress_min": round(int(s.iloc[0]) / 100, 2),
                                       "delayed_min": round(int(dl.iloc[0]) / 100, 2) if len(dl) else None,
                                       "base_minus_stress": round((int(b.iloc[0]) - int(s.iloc[0])) / 100, 2)}
    return {"per_run": per_run, "base_vs_stress_min_group_cash": diff,
            "alerts_by_severity": alerts["severity"].value_counts().sort_index().to_dict()}
