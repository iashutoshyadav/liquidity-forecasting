"""Independent anomaly detectors.

Detectors read ONLY the input dataset (and positions recomputed from it). They never import
or receive ground truth. The same rule functions double as forecast-engine guards.

Rule            Issue type            Decision          Logic
BAL-001         BALANCE_MISMATCH      HELD_FOR_REVIEW   statement closing != opening + sum(ledger) recomputed
FX-001          FX_RATE_MISSING       HELD_FOR_REVIEW   required (currency, business day) rate absent
DUP-001         DUPLICATE_CASH_FLOW   AUTO_RESOLVED     sum(allocation_bp) of a source > 10000, or more than one item
                                                        for the same (source_type, source_id, instalment_no); later
                                                        items excluded (HIGH severity when the duplicate has no link)
CUT-001         CUTOFF_LEAKAGE        REJECTED          basis actual recorded at/after the item (or one of its
                                                        revisions) became known
CUT-003         CUTOFF_LEAKAGE        HELD_FOR_REVIEW   current item values match none of its recorded revisions
                                                        (untracked overwrite: cannot be dated; forecasts ignore it)
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


def _frame(issue: str, table: str, records, observed, expected, details, severity=None, rule=None,
           decision=None) -> pd.DataFrame:
    default_rule, default_decision, default_severity = RULES[issue]
    rule, decision = rule or default_rule, decision or default_decision
    n = len(records)
    sev = default_severity if severity is None else list(severity)
    return pd.DataFrame({"rule_id": rule, "issue_type": issue, "affected_table": table,
                         "affected_record_id": list(records), "severity": sev,
                         "observed_value": list(observed), "expected_value": list(expected),
                         "decision": decision, "details": list(details) if n else []}, columns=DETECTION_COLUMNS)


def _rank_frame(cf: pd.DataFrame, settled: set | None) -> pd.DataFrame:
    """Survivor ranking: settled items first, then earliest known_at, then cf_id."""
    r = cf[["cf_id", "known_at"]].copy()
    r["unsettled"] = 0 if settled is None else (~r["cf_id"].isin(settled)).astype(int)
    return r


def duplicate_excess(cf: pd.DataFrame, links: pd.DataFrame, settled: set | None = None) -> pd.DataFrame:
    """Items whose inclusion pushes a source's allocation above 100%.

    Items are taken in survivor order (settled first, then known_at, cf_id); legitimate instalments
    sum to exactly 10000 bp and identical amounts/dates on *different* sources are never compared.
    """
    l = links.merge(_rank_frame(cf, settled), on="cf_id", how="left")
    l = l.sort_values(["source_type", "source_id", "unsettled", "known_at", "cf_id"], kind="mergesort")
    l["cum_bp"] = l.groupby(["source_type", "source_id"])["allocation_bp"].cumsum()
    l["source_total_bp"] = l.groupby(["source_type", "source_id"])["allocation_bp"].transform("sum")
    first = l.groupby(["source_type", "source_id"])["cf_id"].transform("first")
    out = l[l["cum_bp"] > 10000].copy()
    out["kept_cf_id"] = first[l["cum_bp"] > 10000]
    return out


ITEM_KEY = ["source_type", "source_id", "instalment_no"]


def duplicate_items(cf: pd.DataFrame, links: pd.DataFrame, settled: set | None = None) -> pd.DataFrame:
    """All duplicate forecast items, whether or not they have a forecast_source_link row.

    Two signals are combined: bridge allocation above 100% (``duplicate_excess``) and more than one
    item for the same source instalment ``(source_type, source_id, instalment_no)``. Both use one
    survivor order: items in ``settled`` (that already received cash) first, then (known_at, cf_id);
    the first item is kept. Instalments differ in instalment_no and items on different sources are
    never compared, so neither is flagged.
    """
    cols = ["cf_id", "source_type", "source_id", "instalment_no", "kept_cf_id", "reason", "source_total_bp", "linked"]
    alloc = duplicate_excess(cf, links, settled)
    alloc = alloc.merge(cf[["cf_id", "instalment_no"]], on="cf_id", how="left")
    alloc["reason"] = "ALLOCATION"
    ranked = cf.merge(_rank_frame(cf, settled)[["cf_id", "unsettled"]], on="cf_id", how="left")
    ranked = ranked.sort_values(ITEM_KEY + ["unsettled", "known_at", "cf_id"], kind="mergesort")
    first = ranked.groupby(ITEM_KEY)["cf_id"].transform("first")
    is_dup = ranked.duplicated(ITEM_KEY, keep="first")
    item = ranked.loc[is_dup, ITEM_KEY + ["cf_id"]].copy()
    item["kept_cf_id"] = first[is_dup]
    item["reason"] = "ITEM"
    item["source_total_bp"] = np.nan
    item = item[~item["cf_id"].isin(set(alloc["cf_id"]))]
    out = pd.concat([alloc, item], ignore_index=True)
    out["linked"] = out["cf_id"].isin(set(links["cf_id"]))
    return out.reindex(columns=cols).sort_values("cf_id", kind="mergesort").reset_index(drop=True)


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
    d = duplicate_items(ds["cash_flow_forecast"], ds["forecast_source_link"], set(ds["actual_cash_flow"]["cf_id"]))
    alloc = d["reason"] == "ALLOCATION"
    observed = [str(int(b)) if a else f"duplicate of {k}" for a, b, k in zip(alloc, d["source_total_bp"], d["kept_cf_id"])]
    expected = ["<= 10000" if a else "one item per source instalment" for a in alloc]
    details = [f"{t} {s} already covered by {k}; excluded from forecasts" if a else
               f"{t} {s} instalment {int(n)} already forecast by {k} (same source and instalment)"
               f"{'' if linked else '; item has no forecast_source_link row'}; excluded from forecasts"
               for a, t, s, n, k, linked in zip(alloc, d["source_type"], d["source_id"], d["instalment_no"],
                                                d["kept_cf_id"], d["linked"])]
    severity = ["MEDIUM" if linked else "HIGH" for linked in d["linked"]]
    return _frame("DUPLICATE_CASH_FLOW", "cash_flow_forecast", d["cf_id"], observed, expected, details, severity)


REVISION_FIELDS = ["expected_date", "amount_minor", "currency", "basis_actual_id"]


def unversioned_changes(cf: pd.DataFrame, revisions: pd.DataFrame) -> pd.DataFrame:
    """Items whose current values equal none of their recorded revisions (or that have no revision)."""
    key = lambda df: df[REVISION_FIELDS].astype(object).where(df[REVISION_FIELDS].notna(), "<NA>").astype(str).agg("|".join, axis=1)
    known = set(zip(revisions["cf_id"], key(revisions)))
    mask = [(c, k) not in known for c, k in zip(cf["cf_id"], key(cf))]
    return cf[mask]


def detect_cutoff_leakage(ds: Dataset) -> pd.DataFrame:
    cf, act = ds["cash_flow_forecast"], ds["actual_cash_flow"]
    item = basis_leakage(cf, act)
    rev = ds["cash_flow_forecast_revision"] if "cash_flow_forecast_revision" in ds else None
    rows = {r.cf_id: (f"basis {r.basis_actual_id} recorded {r.basis_recorded_at}", f"recorded before {r.known_at}",
                      "forecast item uses an actual that was not available when the item was known")
            for r in item.itertuples(index=False)}
    if rev is not None:
        for r in basis_leakage(rev, act).itertuples(index=False):
            rows.setdefault(r.cf_id, (f"revision {r.revision_id} basis {r.basis_actual_id} recorded {r.basis_recorded_at}",
                                      f"recorded before {r.known_at}",
                                      f"revision {r.revision_no} uses an actual that was not available when it was recorded"))
    ids_ = sorted(rows)
    out = [_frame("CUTOFF_LEAKAGE", "cash_flow_forecast", ids_, [rows[i][0] for i in ids_], [rows[i][1] for i in ids_],
                  [rows[i][2] for i in ids_])]
    if rev is not None:
        u = unversioned_changes(cf, rev)
        out.append(_frame("CUTOFF_LEAKAGE", "cash_flow_forecast", u["cf_id"],
                          [f"current {d:%Y-%m-%d} / {a}" for d, a in zip(u["expected_date"], u["amount_minor"])],
                          ["values recorded in cash_flow_forecast_revision"] * len(u),
                          ["current values match no recorded revision: change cannot be dated (possible look-ahead); "
                           "forecast runs use revisions only"] * len(u),
                          severity=["MEDIUM"] * len(u), rule="CUT-003", decision="HELD_FOR_REVIEW"))
    return pd.concat(out, ignore_index=True)


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
        note = ("currency differs from account currency; entry quarantined: excluded from balances, forecasts "
                "and variance (statement difference shown as RECONCILING_ITEM)" if table == "ledger_entry"
                else "currency differs from account currency")
        frames.append(_frame("MIXED_CURRENCY", table, bad[key], bad["currency"], bad["account_id"].map(acc_ccy),
                             [note] * len(bad)))
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
