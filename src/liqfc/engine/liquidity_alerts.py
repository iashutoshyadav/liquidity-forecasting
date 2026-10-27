"""Liquidity alerts: projected breaches of the minimum-liquidity policy (ALR-* rules).

ALR-01 a scope (entity or group) breaches when projected closing < min_liquidity (reporting ccy).
ALR-02 one alert per run and scope with first breach date, minimum and days below threshold.
ALR-03 severity CRITICAL when the projected minimum is negative, otherwise WARNING.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def build_alerts(cons: pd.DataFrame, id_year: int) -> pd.DataFrame:
    rows = []
    for (rid, level, scope), g in cons.groupby(["forecast_run_id", "level", "scope_id"], sort=True):
        g = g.sort_values("forecast_date")
        below = g[g["below_threshold"]]
        if below.empty:
            continue
        imin = g["closing_reporting_minor"].idxmin()
        mn = int(g.loc[imin, "closing_reporting_minor"])
        rows.append({"forecast_run_id": rid, "level": level, "scope_id": scope,
                     "threshold_reporting_minor": int(g["min_liquidity_reporting_minor"].iloc[0]),
                     "min_projected_reporting_minor": mn, "min_projected_date": g.loc[imin, "forecast_date"],
                     "first_breach_date": below["forecast_date"].iloc[0], "days_below_threshold": int(len(below)),
                     "severity": "CRITICAL" if mn < 0 else "WARNING"})
    cols = ["forecast_run_id", "level", "scope_id", "threshold_reporting_minor", "min_projected_reporting_minor",
            "min_projected_date", "first_breach_date", "days_below_threshold", "severity"]
    out = pd.DataFrame(rows, columns=cols)
    out.insert(0, "alert_id", [f"ALR-{id_year}-{i:06d}" for i in range(1, len(out) + 1)])
    for c in ["threshold_reporting_minor", "min_projected_reporting_minor", "days_below_threshold"]:
        out[c] = out[c].astype(np.int64)
    return out
