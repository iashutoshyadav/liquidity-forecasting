"""ActualCashFlow: one row per settlement ledger entry, linked to the forecast item it settles."""
from __future__ import annotations

import pandas as pd

from liqfc.core import ids
from liqfc.generators.context import GenContext


def build_actuals(ctx: GenContext, ledger: pd.DataFrame, cf: pd.DataFrame) -> pd.DataFrame:
    s = ctx.settings
    src = ledger[ledger["source_type"].notna()].copy()
    key = cf.set_index(["source_type", "source_id", "instalment_no"])["cf_id"]
    src["cf_id"] = [key[(t, i, int(n))] for t, i, n in zip(src["source_type"], src["source_id"], src["instalment_no"])]
    src = src.sort_values("ledger_entry_id").reset_index(drop=True)
    return pd.DataFrame({
        "actual_id": ids.seq_ids("ACT", s.id_year, len(src)),
        "cf_id": src["cf_id"], "source_type": src["source_type"], "source_id": src["source_id"],
        "ledger_entry_id": src["ledger_entry_id"], "account_id": src["account_id"], "currency": src["currency"],
        "amount_minor": src["amount_minor"], "actual_date": src["value_date"], "recorded_at": src["recorded_at"],
    })
