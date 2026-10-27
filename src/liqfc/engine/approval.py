"""Forecast run approval policy (RUN-01).

A run stays EXECUTED (held for review) instead of APPROVED when either:
* an account in the run converted its opening balance with a STALE_FALLBACK or MISSING FX rate; or
* an account in the run has an unexplained statement BREAK dated within ``balance_break_lookback_days``
  before the cutoff whose size, in reporting currency at that day's rate, is at least
  ``balance_break_materiality_reporting_minor``. A break that cannot be converted counts as material.

Older breaks do not hold a run: its opening balance comes from the ledger, and a break followed by
reconciled statements does not affect it. RECONCILING_ITEM days (quarantined entries) are not breaks.
"""
from __future__ import annotations

import pandas as pd

from liqfc.core import money


def material_breaks(positions: pd.DataFrame, settings) -> pd.DataFrame:
    """BREAK account-days with their reporting-currency size and materiality flag."""
    mu, rep = settings.minor_units, settings.reporting_currency
    threshold = int(settings.forecast["approval_policy"]["balance_break_materiality_reporting_minor"])
    b = positions.loc[positions["reconciliation_status"] == "BREAK",
                      ["account_id", "position_date", "currency", "reconciliation_difference_minor",
                       "explained_difference_minor", "rate_e8"]].copy()
    b["unexplained"] = b["reconciliation_difference_minor"] - b["explained_difference_minor"].fillna(0)
    sizes = []
    for ccy, diff, rate in zip(b["currency"], b["unexplained"], b["rate_e8"]):
        if pd.isna(diff):
            sizes.append(None)
        elif ccy == rep:
            sizes.append(abs(int(diff)))
        elif rate is None or pd.isna(rate):
            sizes.append(None)
        else:
            sizes.append(abs(money.convert(int(diff), mu[ccy], int(rate), mu[rep])))
    b["size_reporting_minor"] = sizes
    b["material"] = [s is None or s >= threshold for s in sizes]
    return b


def run_holds(spec, run_accounts: pd.DataFrame, breaks: pd.DataFrame, settings) -> list[str]:
    """Reasons that hold ``spec``'s run; empty list means it can be approved."""
    policy = settings.forecast["approval_policy"]
    reasons = []
    ra = run_accounts[run_accounts["forecast_run_id"] == spec.run_id]
    stale = ra[ra["fx_rate_status"].isin(["STALE_FALLBACK", "MISSING"])]
    for a, s in zip(stale["account_id"], stale["fx_rate_status"]):
        reasons.append(f"{a}: opening converted with {s} FX rate")
    lo = spec.cutoff - pd.Timedelta(days=int(policy["balance_break_lookback_days"]))
    hit = breaks[breaks["material"] & breaks["account_id"].isin(ra["account_id"])
                 & (breaks["position_date"] >= lo) & (breaks["position_date"] < spec.cutoff)]
    for a, d, s in zip(hit["account_id"], hit["position_date"], hit["size_reporting_minor"]):
        size = "unconvertible" if s is None else f"{money.format_minor(int(s), settings.minor_units[settings.reporting_currency])} {settings.reporting_currency}"
        reasons.append(f"{a}: statement break on {d:%Y-%m-%d} ({size})")
    return reasons
