"""Independent anomaly detectors.

Detectors read ONLY the input dataset (and positions recomputed from it). They never import
or receive ground truth. The same rule functions double as forecast-engine guards.

Rule            Issue type            Decision          Logic
BAL-001         BALANCE_MISMATCH      HELD_FOR_REVIEW   statement closing != opening + sum(ledger) recomputed
FX-001          FX_RATE_MISSING       HELD_FOR_REVIEW   required (currency, business day) rate absent
DUP-001         DUPLICATE_CASH_FLOW   AUTO_RESOLVED     sum(allocation_bp) of a source > 10000; later items excluded
CUT-001         CUTOFF_LEAKAGE        REJECTED          basis actual recorded after the item became known
CCY-001         MIXED_CURRENCY        REJECTED          item currency != account currency (cannot be summed)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids
from liqfc.core.dataset import Dataset

DETECTION_COLUMNS = ["rule_id", "issue_type", "affected_table", "affected_record_id", "severity",
                     "observed_value", "expected_value", "decision", "details"]
RULES = {
    "BALANCE_MISMATCH": ("BAL-001", "HELD_FOR_REVIEW", "HIGH"),
    "FX_RATE_MISSING": ("FX-001", "HELD_FOR_REVIEW", "MEDIUM"),
    "DUPLICATE_CASH_FLOW": ("DUP-001", "AUTO_RESOLVED", "MEDIUM"),
    "CUTOFF_LEAKAGE": ("CUT-001", "REJECTED", "HIGH"),
    "MIXED_CURRENCY": ("CCY-001", "REJECTED", "HIGH"),
}


def _frame(issue: str, table: str, records, observed, expected, details) -> pd.DataFrame:
    rule, decision, severity = RULES[issue]
    n = len(records)
    return pd.DataFrame({"rule_id": rule, "issue_type": issue, "affected_table": table,
                         "affected_record_id": list(records), "severity": severity,
                         "observed_value": list(observed), "expected_value": list(expected),
                         "decision": decision, "details": list(details) if n else []}, columns=DETECTION_COLUMNS)


def duplicate_excess(cf: pd.DataFrame, links: pd.DataFrame) -> pd.DataFrame:
    """Items whose inclusion pushes a source's allocation above 100%.

    Items are taken in (known_at, cf_id) order; legitimate instalments sum to exactly 10000 bp
    and identical amounts/dates on *different* sources are never compared.
    """
    l = links.merge(cf[["cf_id", "known_at"]], on="cf_id", how="left")
    l = l.sort_values(["source_type", "source_id", "known_at", "cf_id"], kind="mergesort")
    l["cum_bp"] = l.groupby(["source_type", "source_id"])["allocation_bp"].cumsum()
    l["source_total_bp"] = l.groupby(["source_type", "source_id"])["allocation_bp"].transform("sum")
    first = l.groupby(["source_type", "source_id"])["cf_id"].transform("first")
    out = l[l["cum_bp"] > 10000].copy()
    out["kept_cf_id"] = first[l["cum_bp"] > 10000]
    return out


def currency_mismatch(cf: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    acc_ccy = cf["account_id"].map(accounts.set_index("account_id")["currency"])
    out = cf[cf["currency"].values != acc_ccy.values].copy()
    out["account_currency"] = acc_ccy[out.index]
    return out


def basis_leakage(cf: pd.DataFrame, actuals: pd.DataFrame, reference_ts=None) -> pd.DataFrame:
    """Items whose basis actual was recorded at/after the item's known_at (or a run cutoff)."""
    b = cf[cf["basis_actual_id"].notna()].merge(
        actuals[["actual_id", "recorded_at"]].rename(columns={"actual_id": "basis_actual_id", "recorded_at": "basis_recorded_at"}),
        on="basis_actual_id", how="left")
    ref = b["known_at"] if reference_ts is None else pd.Series(pd.Timestamp(reference_ts), index=b.index)
    return b[b["basis_recorded_at"].isna() | (b["basis_recorded_at"] >= ref)]


def missing_rates(accounts: pd.DataFrame, rates: pd.DataFrame, cal, start, end, reporting: str) -> pd.DataFrame:
    needed = sorted(c for c in accounts["currency"].unique() if c != reporting)
    bdays = pd.to_datetime(cal.business_days(start, end))
    have = set(zip(rates["base_currency"], pd.to_datetime(rates["rate_date"])))
    rows = [(ids.rate_id(d, c, reporting), c, d) for c in needed for d in bdays if (c, d) not in have]
    return pd.DataFrame(rows, columns=["rate_id", "currency", "rate_date"])


def detect_balance_mismatch(ds: Dataset, positions: pd.DataFrame) -> pd.DataFrame:
    st = ds["account_balance_statement"][["statement_id", "account_id", "statement_date"]]
    brk = positions[positions["reconciliation_status"] == "BREAK"].merge(
        st, left_on=["account_id", "position_date"], right_on=["account_id", "statement_date"], how="left")
    return _frame("BALANCE_MISMATCH", "account_balance_statement", brk["statement_id"].fillna("MISSING_STATEMENT"),
                  brk["statement_closing_minor"].astype(str), brk["closing_balance_minor"].astype(str),
                  [f"{a} {d:%Y-%m-%d} difference {x}" for a, d, x in
                   zip(brk["account_id"], brk["position_date"], brk["reconciliation_difference_minor"])])


def detect_missing_fx(ds: Dataset, cal, settings) -> pd.DataFrame:
    m = missing_rates(ds["bank_account"], ds["currency_rate"], cal, settings.start, settings.end, settings.reporting_currency)
    return _frame("FX_RATE_MISSING", "currency_rate", m["rate_id"], ["missing"] * len(m), ["rate present"] * len(m),
                  [f"{c} rate for business day {d:%Y-%m-%d} absent; prior-rate fallback applied and held for review"
                   for c, d in zip(m["currency"], m["rate_date"])])


def detect_duplicates(ds: Dataset) -> pd.DataFrame:
    d = duplicate_excess(ds["cash_flow_forecast"], ds["forecast_source_link"])
    return _frame("DUPLICATE_CASH_FLOW", "cash_flow_forecast", d["cf_id"], d["source_total_bp"].astype(str),
                  ["<= 10000"] * len(d),
                  [f"{t} {s} already covered by {k}; excluded from forecasts" for t, s, k in
                   zip(d["source_type"], d["source_id"], d["kept_cf_id"])])


def detect_cutoff_leakage(ds: Dataset) -> pd.DataFrame:
    b = basis_leakage(ds["cash_flow_forecast"], ds["actual_cash_flow"])
    return _frame("CUTOFF_LEAKAGE", "cash_flow_forecast", b["cf_id"],
                  [f"basis {a} recorded {t}" for a, t in zip(b["basis_actual_id"], b["basis_recorded_at"])],
                  [f"recorded before {k}" for k in b["known_at"]],
                  ["forecast item uses an actual that was not available when the item was known"] * len(b))


def detect_mixed_currency(ds: Dataset) -> pd.DataFrame:
    frames = []
    m = currency_mismatch(ds["cash_flow_forecast"], ds["bank_account"])
    frames.append(_frame("MIXED_CURRENCY", "cash_flow_forecast", m["cf_id"], m["currency"], m["account_currency"],
                         [f"{c} amount on {a} account {acc} would be aggregated without conversion"
                          for c, a, acc in zip(m["currency"], m["account_currency"], m["account_id"])]))
    acc_ccy = ds["bank_account"].set_index("account_id")["currency"]
    for table, key in [("ledger_entry", "ledger_entry_id"), ("actual_cash_flow", "actual_id"),
                       ("receivable", "receivable_id"), ("payable", "payable_id")]:
        t = ds[table]
        bad = t[t["currency"].values != t["account_id"].map(acc_ccy).values]
        frames.append(_frame("MIXED_CURRENCY", table, bad[key], bad["currency"], bad["account_id"].map(acc_ccy),
                             ["currency differs from account currency"] * len(bad)))
    return pd.concat(frames, ignore_index=True)


def detect_all(ds: Dataset, positions: pd.DataFrame, cal, settings) -> pd.DataFrame:
    out = pd.concat([
        detect_balance_mismatch(ds, positions), detect_missing_fx(ds, cal, settings), detect_duplicates(ds),
        detect_cutoff_leakage(ds), detect_mixed_currency(ds),
    ], ignore_index=True)
    return out.sort_values(["rule_id", "affected_table", "affected_record_id"], kind="mergesort").reset_index(drop=True)


def flagged_cf_ids(detections: pd.DataFrame) -> set[str]:
    d = detections[(detections["affected_table"] == "cash_flow_forecast")]
    return set(d["affected_record_id"])
