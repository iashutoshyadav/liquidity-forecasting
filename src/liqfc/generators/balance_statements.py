"""AccountBalanceStatement: the bank's own end-of-day view, built from its transaction feed.

The bank feed is the ledger's value-dated movements; statements are produced by a separate
code path from engine/balances.py so the treasury calculation can be reconciled against it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids
from liqfc.generators.context import GenContext


def build_statements(ctx: GenContext, accounts: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    s = ctx.settings
    days = pd.date_range(s.start, s.end, freq="D")
    rows = []
    feed = {aid: g for aid, g in ledger.groupby("account_id")}
    for r in accounts.sort_values("account_id").itertuples(index=False):
        g = feed.get(r.account_id)
        credits = dict.fromkeys(days, 0)
        debits = dict.fromkeys(days, 0)
        count = dict.fromkeys(days, 0)
        if g is not None:
            for d, a in zip(g["value_date"], g["amount_minor"]):
                if a > 0:
                    credits[d] += int(a)
                else:
                    debits[d] += -int(a)
                count[d] += 1
        bal = int(r.opening_balance_minor)
        for d in days:
            close = bal + credits[d] - debits[d]
            rows.append((r.account_id, d, r.currency, bal, credits[d], debits[d], close, count[d]))
            bal = close
    st = pd.DataFrame(rows, columns=["account_id", "statement_date", "currency", "opening_balance_minor",
                                     "total_credits_minor", "total_debits_minor", "closing_balance_minor", "entry_count"])
    st = st.sort_values(["statement_date", "account_id"], kind="mergesort").reset_index(drop=True)
    st.insert(0, "statement_id", ids.seq_ids("STMT", s.id_year, len(st)))
    st["source"] = "SYNTHETIC_BANK_FEED"
    for c in ["opening_balance_minor", "total_credits_minor", "total_debits_minor", "closing_balance_minor"]:
        st[c] = st[c].astype(np.int64)
    return st
