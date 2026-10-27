"""Labelled anomaly injection (runs only after the clean baseline has been validated).

Each injector changes only its own target records and writes one ground-truth row per target
with the original and injected values. Targets are chosen from a dedicated RNG stream and
are disjoint across injectors.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.anomalies.ground_truth import GroundTruth
from liqfc.core import ids, money
from liqfc.core.dataset import Dataset
from liqfc.core.rng import stream


def inject_all(baseline: Dataset, settings, cal, fx) -> tuple[Dataset, pd.DataFrame]:
    ds = baseline.copy(label="injected")
    gt = GroundTruth()
    counts = settings.anomaly_counts
    used_cf: set[str] = set()
    _balance_mismatch(ds, settings, fx, gt, counts["BALANCE_MISMATCH"])
    _missing_fx(ds, settings, cal, gt, counts["FX_RATE_MISSING"])
    _duplicates(ds, settings, gt, counts["DUPLICATE_CASH_FLOW"], used_cf)
    _cutoff_leakage(ds, settings, gt, counts["CUTOFF_LEAKAGE"], used_cf)
    _mixed_currency(ds, settings, fx, gt, counts["MIXED_CURRENCY"], used_cf)
    return ds.conform(), gt.frame()


def _rng(settings, name):
    return stream(settings.seed, f"anomalies:{name}")


def _usd_to_local(settings, fx, usd_minor: int, ccy: str, date) -> int:
    rep = settings.reporting_currency
    if ccy == rep:
        return usd_minor
    rate, _, _ = fx.effective(ccy, date)
    mu = settings.minor_units
    return money.convert_inverse(usd_minor, mu[rep], rate, mu[ccy])


def _balance_mismatch(ds, settings, fx, gt, k):
    rng = _rng(settings, "balance")
    st = ds["account_balance_statement"]
    idx = np.sort(rng.choice(len(st), size=k, replace=False))
    lo, hi = settings.raw["anomalies"]["balance_delta_reporting_range"]
    for i in idx:
        r = st.iloc[i]
        delta = _usd_to_local(settings, fx, int(rng.uniform(lo, hi) * 100), r["currency"], r["statement_date"])
        delta = max(delta, 1) * (1 if rng.random() < 0.5 else -1)
        new = int(r["closing_balance_minor"]) + delta
        st.iat[i, st.columns.get_loc("closing_balance_minor")] = new
        gt.add("BALANCE_MISMATCH", "account_balance_statement", r["statement_id"], "closing_balance_minor",
               int(r["closing_balance_minor"]), new, "BAL-001", "HELD_FOR_REVIEW",
               f"bank-reported closing shifted by {delta} minor units on {r['account_id']} {r['statement_date']:%Y-%m-%d}")


def _missing_fx(ds, settings, cal, gt, k):
    rng = _rng(settings, "fx")
    rates = ds["currency_rate"]
    margin = pd.Timedelta(days=int(settings.raw["anomalies"]["fx_missing_window_margin_days"]))
    pool = rates[(rates["rate_date"] >= settings.start + margin) & (rates["rate_date"] <= settings.end - margin)]
    pick = pool.iloc[np.sort(rng.choice(len(pool), size=k, replace=False))]
    for r in pick.itertuples(index=False):
        gt.add("FX_RATE_MISSING", "currency_rate", r.rate_id, "rate", r.rate, "<missing>", "FX-001", "HELD_FOR_REVIEW",
               f"{r.base_currency}/{r.quote_currency} rate for {r.rate_date:%Y-%m-%d} removed")
    ds["currency_rate"] = rates[~rates["rate_id"].isin(pick["rate_id"])].reset_index(drop=True)


def _cf_pool(ds, settings, used):
    cf = ds["cash_flow_forecast"]
    first_cut = pd.Timestamp(min(settings.forecast["cutoff_dates"]))
    last_cut = pd.Timestamp(max(settings.forecast["cutoff_dates"]))
    return cf[(cf["expected_date"] >= first_cut) & (cf["known_at"] < last_cut) & cf["status"].isin(["OPEN", "SETTLED"])
              & (cf["flow_category"] != "PAYROLL") & ~cf["cf_id"].isin(used)]


def _duplicates(ds, settings, gt, k, used):
    rng = _rng(settings, "duplicate")
    cf, links = ds["cash_flow_forecast"], ds["forecast_source_link"]
    pool = _cf_pool(ds, settings, used)
    pick = pool.iloc[np.sort(rng.choice(len(pool), size=k, replace=False))]
    n_cf, n_link = len(cf), len(links)
    new_cf, new_links = [], []
    for j, (_, r) in enumerate(pick.iterrows(), start=1):
        new_id = ids.seq_id("CF", settings.id_year, n_cf + j)
        dup = r.copy()
        dup["cf_id"] = new_id
        dup["status"] = "OPEN"
        new_cf.append(dup)
        src = links[links["cf_id"] == r["cf_id"]].iloc[0]
        new_links.append({"link_id": ids.seq_id("FSL", settings.id_year, n_link + j), "cf_id": new_id,
                          "source_type": src["source_type"], "source_id": src["source_id"], "allocation_bp": src["allocation_bp"]})
        used.add(r["cf_id"])
        gt.add("DUPLICATE_CASH_FLOW", "cash_flow_forecast", new_id, "cf_id", r["cf_id"], new_id, "DUP-001",
               "AUTO_RESOLVED", f"second forecast inclusion of {r['source_type']} {r['source_id']}")
    ds["cash_flow_forecast"] = pd.concat([cf, pd.DataFrame(new_cf)], ignore_index=True)
    ds["forecast_source_link"] = pd.concat([links, pd.DataFrame(new_links)], ignore_index=True)


def _cutoff_leakage(ds, settings, gt, k, used):
    """A forecast item re-estimated from its own future settlement (hindsight)."""
    rng = _rng(settings, "cutoff")
    cf, act = ds["cash_flow_forecast"], ds["actual_cash_flow"]
    H = pd.Timedelta(days=int(settings.forecast["horizon_days"]))
    first_act = act.sort_values(["recorded_at", "actual_id"]).drop_duplicates("cf_id").set_index("cf_id")
    pool = _cf_pool(ds, settings, used)
    pool = pool[pool["flow_category"].eq("CUSTOMER_RECEIPT") & pool["cf_id"].isin(first_act.index)]
    ok = []
    for r in pool.itertuples(index=False):
        a = first_act.loc[r.cf_id]
        for c in (pd.Timestamp(x) for x in settings.forecast["cutoff_dates"]):
            if r.known_at < c <= a["recorded_at"] and r.expected_date < c + H:
                ok.append(r.cf_id)
                break
    pick = sorted(rng.choice(ok, size=k, replace=False))
    for cid in pick:
        i = cf.index[cf["cf_id"] == cid][0]
        a = first_act.loc[cid]
        orig = cf.loc[i, ["basis_actual_id", "expected_date", "amount_minor"]].to_dict()
        cf.at[i, "basis_actual_id"] = a["actual_id"]
        cf.at[i, "expected_date"] = a["actual_date"]
        cf.at[i, "amount_minor"] = int(a["amount_minor"])
        used.add(cid)
        gt.add("CUTOFF_LEAKAGE", "cash_flow_forecast", cid, "basis_actual_id", orig["basis_actual_id"], a["actual_id"],
               "CUT-001", "REJECTED",
               f"expected_date {orig['expected_date']:%Y-%m-%d}->{a['actual_date']:%Y-%m-%d}, amount "
               f"{orig['amount_minor']}->{int(a['amount_minor'])} copied from actual recorded {a['recorded_at']:%Y-%m-%dT%H:%M:%SZ}")


def _mixed_currency(ds, settings, fx, gt, k, used):
    rng = _rng(settings, "currency")
    cf = ds["cash_flow_forecast"]
    pool = _cf_pool(ds, settings, used)
    pick = pool.iloc[np.sort(rng.choice(len(pool), size=k, replace=False))]
    rep, mu = settings.reporting_currency, settings.minor_units
    for r in pick.itertuples(index=False):
        new_ccy = str(rng.choice([c for c in mu if c != r.currency]))
        d = r.known_at.normalize()
        r_old = money.RATE_SCALE if r.currency == rep else fx.effective(r.currency, d)[0]
        usd = money.convert(int(r.amount_minor), mu[r.currency], r_old, mu[rep])
        new_amt = usd if new_ccy == rep else money.convert_inverse(usd, mu[rep], fx.effective(new_ccy, d)[0], mu[new_ccy])
        i = cf.index[cf["cf_id"] == r.cf_id][0]
        cf.at[i, "currency"] = new_ccy
        cf.at[i, "amount_minor"] = int(new_amt) if new_amt != 0 else int(np.sign(r.amount_minor))
        used.add(r.cf_id)
        gt.add("MIXED_CURRENCY", "cash_flow_forecast", r.cf_id, "currency", r.currency, new_ccy, "CCY-001", "REJECTED",
               f"amount restated {r.amount_minor} {r.currency} -> {int(new_amt)} {new_ccy} on a {r.currency} account")
