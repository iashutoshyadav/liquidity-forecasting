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
