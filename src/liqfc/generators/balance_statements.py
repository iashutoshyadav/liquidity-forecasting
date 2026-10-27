"""Independent bank-side model: bank statement lines and daily statements (BNK-* rules).

The bank sees the same economic cash events as treasury, but records them by its own rules.
Nothing here reads ledger rows or ledger ids.

BNK-01 most events are booked by the bank on their value date, for the same amount;
BNK-02 a share of receipts (``clearing_lag_types``) clear 1..``clearing_lag_max_business_days`` business
       days later; a line that would be booked after the data end is not on the statements yet;
BNK-03 a share of outgoing payments (``bank_fee_types``) carry a wire charge the bank books on the same
       day; treasury never records it in the ledger (a bank-only item);
BNK-04 statements: opening(start) = account opening balance; closing(d) = opening(d) + credits(d) -
       debits(d) over lines booked on d.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids
from liqfc.generators.context import GenContext

BANK_CODE = {"BANK_FEE": "FEE", "INTEREST": "INTEREST", "INTERNAL_TRANSFER_IN": "TRANSFER", "INTERNAL_TRANSFER_OUT": "TRANSFER"}


def build_bank_lines(ctx: GenContext, accounts: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    s = ctx.settings
    cfg = s.gen["bank"]
    cal = ctx.cal
    acc = accounts.set_index("account_id")
    ev = events.copy()
    ev["booking_date"] = ev["value_date"]

    rng_c = ctx.rng("bank:clearing")
    u, lag = rng_c.random(len(ev)), rng_c.integers(1, int(cfg["clearing_lag_max_business_days"]) + 1, len(ev))
    late = ev["entry_type"].isin(cfg["clearing_lag_types"]).to_numpy() & (u < float(cfg["clearing_lag_probability"]))
    shifted = pd.to_datetime(cal.add_business_days(ev["value_date"], lag))
    ev.loc[late, "booking_date"] = shifted[late]
    ev = ev[ev["booking_date"] <= s.end]
    ev["bank_tx_code"] = ev["entry_type"].map(BANK_CODE).fillna(
        pd.Series(np.where(ev["amount_minor"] > 0, "RECEIPT", "PAYMENT"), index=ev.index))

    rng_f = ctx.rng("bank:fees")
    payments = ev[ev["entry_type"].isin(cfg["bank_fee_types"])]
    pick = payments[rng_f.random(len(payments)) < float(cfg["bank_fee_probability"])]
    lo, hi = cfg["bank_fee_reporting_range"]
    usd = rng_f.uniform(lo, hi, len(pick))
    fee = np.maximum(ctx.usd_to_local_minor(usd, pick["currency"].to_numpy(), pick["value_date"].to_numpy()), 1)
    fees = pd.DataFrame({
        "account_id": pick["account_id"].to_numpy(), "value_date": pick["value_date"].to_numpy(),
        "booking_date": pick["booking_date"].to_numpy(), "currency": pick["currency"].to_numpy(),
        "amount_minor": -fee.astype(np.int64), "bank_tx_code": "FEE",
        "counterparty_ref": [acc.at[a, "bank_name"] for a in pick["account_id"]],
        "description": "Wire transfer charge"})
    lines = pd.concat([ev[["account_id", "value_date", "booking_date", "currency", "amount_minor", "bank_tx_code",
                           "counterparty_ref", "description"]], fees], ignore_index=True)
    lines["x_seq"] = np.arange(len(lines))
    lines = lines.sort_values(["booking_date", "account_id", "bank_tx_code", "amount_minor", "x_seq"],
                              kind="mergesort").reset_index(drop=True)
    lines.insert(0, "bank_line_id", [ids.seq_id("BSL", s.id_year, i, 8) for i in range(1, len(lines) + 1)])
    lines["amount_minor"] = lines["amount_minor"].astype(np.int64)
    return lines.drop(columns="x_seq")


def build_statements(ctx: GenContext, accounts: pd.DataFrame, bank_lines: pd.DataFrame) -> pd.DataFrame:
    s = ctx.settings
    days = pd.date_range(s.start, s.end, freq="D")
    grid = pd.MultiIndex.from_product([sorted(accounts["account_id"]), days], names=["account_id", "statement_date"])
    amt = bank_lines["amount_minor"].astype(np.int64)
    agg = pd.DataFrame({"account_id": bank_lines["account_id"], "statement_date": bank_lines["booking_date"],
                        "total_credits_minor": amt.clip(lower=0), "total_debits_minor": (-amt).clip(lower=0),
                        "entry_count": 1}).groupby(["account_id", "statement_date"]).sum()
    st = agg.reindex(grid, fill_value=0).reset_index()
    net = st["total_credits_minor"] - st["total_debits_minor"]
    opening = st["account_id"].map(accounts.set_index("account_id")["opening_balance_minor"]).astype(np.int64)
    st["closing_balance_minor"] = opening + net.groupby(st["account_id"]).cumsum()
    st["opening_balance_minor"] = st["closing_balance_minor"] - net
    st["currency"] = st["account_id"].map(accounts.set_index("account_id")["currency"])
    st = st.sort_values(["statement_date", "account_id"], kind="mergesort").reset_index(drop=True)
    st.insert(0, "statement_id", ids.seq_ids("STMT", s.id_year, len(st)))
    st["source"] = "SYNTHETIC_BANK_FEED"
    for c in ["opening_balance_minor", "total_credits_minor", "total_debits_minor", "closing_balance_minor", "entry_count"]:
        st[c] = st[c].astype(np.int64)
    return st
