"""CashFlowForecast + ForecastSourceLink generation.

IDs are assigned after sorting by (known_date, expected_date, source_type, source_id, instalment_no).
Status and basis_actual_id are derived later from actual settlements (CF-* rules).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids
from liqfc.generators.context import GenContext


def assign_cf_ids(ctx: GenContext, items: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    s = ctx.settings
    hour = int(s.gen["forecast_item"]["known_hour_utc"])
    cf = items.sort_values(["known_date", "expected_date", "source_type", "source_id", "instalment_no"],
                           kind="mergesort").reset_index(drop=True)
    cf.insert(0, "cf_id", ids.seq_ids("CF", s.id_year, len(cf)))
    cf["known_at"] = cf["known_date"] + pd.Timedelta(hours=hour)
    cf["basis_actual_id"] = None
    cf["status"] = "OPEN"
    links = pd.DataFrame({
        "link_id": ids.seq_ids("FSL", s.id_year, len(cf)),
        "cf_id": cf["cf_id"], "source_type": cf["source_type"], "source_id": cf["source_id"],
        "allocation_bp": cf["allocation_bp"].astype(np.int64),
    })
    return cf, links


def finalize_cf(cf: pd.DataFrame, actuals: pd.DataFrame, receivables: pd.DataFrame, payables: pd.DataFrame) -> pd.DataFrame:
    cf = cf.copy()
    pay = payables[payables["category"] == "PAYROLL"].sort_values(["entity_id", "scheduled_date"])
    prev_pay = pay.groupby("entity_id")["payable_id"].shift(1)
    act_by_src = actuals.drop_duplicates("source_id").set_index("source_id")["actual_id"]
    basis = pd.Series(prev_pay.map(act_by_src).values, index=pay["payable_id"].values)
    is_payroll = cf["source_id"].isin(basis.index)
    cf.loc[is_payroll, "basis_actual_id"] = cf.loc[is_payroll, "source_id"].map(basis)
    cf["basis_actual_id"] = cf["basis_actual_id"].where(cf["basis_actual_id"].notna(), None)

    settled = actuals.groupby("cf_id")["amount_minor"].sum()
    got = cf["cf_id"].map(settled).fillna(0).astype(np.int64)
    status = np.where(got == 0, "OPEN", np.where(got.abs() >= cf["amount_minor"].abs(), "SETTLED", "PARTIALLY_SETTLED"))
    status = np.where((got != 0) & cf["flow_category"].eq("PAYROLL"), "SETTLED", status)
    rec_status = receivables.set_index("receivable_id")["status"]
    pay_status = payables.set_index("payable_id")["status"]
    src_status = cf["source_id"].map(rec_status).fillna(cf["source_id"].map(pay_status))
    status = np.where((src_status == "WRITTEN_OFF") & (got == 0), "WRITTEN_OFF", status)
    status = np.where(src_status == "CANCELLED", "CANCELLED", status)
    cf["status"] = status
    return cf


REVISION_VALUE_FIELDS = ["expected_date", "amount_minor", "currency", "basis_actual_id"]


def build_revisions(ctx: GenContext, cf: pd.DataFrame, actuals: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """CF-04 append-only revision history.

    Every item has revision 1 (its original values, known at the item's known_at). A share of
    single-instalment customer receipts gets a CUSTOMER_PROMISE revision: a new expected date,
    recorded at a time strictly after the item became known and strictly before its first
    settlement was recorded (or before the original expected date if unsettled). The current
    cash_flow_forecast row is set to the latest revision.
    """
    s = ctx.settings
    cal = ctx.cal
    rng = ctx.rng("forecast_items:revisions")
    share = float(s.gen["forecast_item"]["promise_revision_share"])
    hour = int(s.gen["forecast_item"]["known_hour_utc"])
    revs = cf[["cf_id", "known_at"] + REVISION_VALUE_FIELDS].copy()
    revs["revision_no"], revs["reason"] = 1, "INITIAL"

    per_source = cf.groupby(["source_type", "source_id"])["cf_id"].transform("size")
    pool = cf[(cf["flow_category"] == "CUSTOMER_RECEIPT") & (per_source == 1)]
    first = actuals.sort_values(["recorded_at", "actual_id"]).drop_duplicates("cf_id").set_index("cf_id")
    u_pick, u_time, noise, slip = (rng.random(len(pool)), rng.random(len(pool)),
                                   rng.normal(0, 1.5, len(pool)), rng.integers(3, 15, len(pool)))
    extra = []
    for k, r in enumerate(pool.itertuples(index=False)):
        if u_pick[k] >= share:
            continue
        lo = r.known_at.normalize() + pd.Timedelta(days=1)
        if r.cf_id in first.index:
            hi = first.at[r.cf_id, "recorded_at"].normalize() - pd.Timedelta(days=1)
            target = first.at[r.cf_id, "actual_date"] + pd.Timedelta(days=int(round(noise[k])))
        else:
            hi = min(r.expected_date, s.end)
            target = r.expected_date + pd.Timedelta(days=int(slip[k]))
        if hi <= lo:
            continue
        day = lo + pd.Timedelta(days=int(u_time[k] * (hi - lo).days))
        new_date = pd.Timestamp(cal.roll_forward([target])[0])
        if new_date == r.expected_date:
            continue
        extra.append({"cf_id": r.cf_id, "known_at": day + pd.Timedelta(hours=hour + 1), "expected_date": new_date,
                      "amount_minor": r.amount_minor, "currency": r.currency, "basis_actual_id": None,
                      "revision_no": 2, "reason": "CUSTOMER_PROMISE"})
    revs = pd.concat([revs, pd.DataFrame(extra)], ignore_index=True)
    revs = revs.sort_values(["known_at", "cf_id", "revision_no"], kind="mergesort").reset_index(drop=True)
    revs.insert(0, "revision_id", ids.seq_ids("CFR", s.id_year, len(revs)))
    latest = revs.sort_values(["cf_id", "revision_no"]).drop_duplicates("cf_id", keep="last").set_index("cf_id")
    cf = cf.copy()
    for col in REVISION_VALUE_FIELDS:
        cf[col] = cf["cf_id"].map(latest[col]).where(cf["cf_id"].map(latest[col]).notna(), None) \
            if col == "basis_actual_id" else cf["cf_id"].map(latest[col])
    return revs, cf
