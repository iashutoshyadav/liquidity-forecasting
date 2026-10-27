"""Bank reconciliation: match ledger entries to bank statement lines (REC-* rules).

REC-01 a ledger entry matches one bank line on the same account and exact amount, booked on the ledger
       value date or up to ``match_window_business_days`` business days later; pairs are taken in
       (ledger date, business-day lag, ledger id, bank date, bank line id) order, one-to-one.
REC-02 statuses: MATCHED (same day); TIMING_DIFFERENCE (bank later, within the window); BANK_ONLY (bank
       line with no ledger entry that is a bank charge, code FEE, or corresponds to a currency-quarantined
       ledger entry); LEDGER_ONLY (ledger entry in transit: its clearing window runs past the data end);
       UNMATCHED_BANK / UNMATCHED_LEDGER (no match and no documented reason: genuine breaks).
REC-03 explained difference (statement - ledger) on day d = sum of BANK_ONLY lines booked <= d
       - sum of LEDGER_ONLY entries dated <= d - sum of TIMING_DIFFERENCE amounts with ledger date
       <= d < bank date. UNMATCHED_* items are never explained. A day is RECONCILED when the difference
       is 0, RECONCILING_ITEM when it equals the explained amount, and BREAK otherwise.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EXPLAIN = {
    "MATCHED": "Same amount on the same day in the ledger and on the bank statement",
    "TIMING_DIFFERENCE": "Booked by the bank after the ledger value date (clearing delay)",
    "BANK_ONLY": "On the bank statement with no ledger entry (e.g. unrecorded bank charge or quarantined entry)",
    "LEDGER_ONLY": "In transit: in the ledger, clearing window runs past the last statement date",
    "UNMATCHED_BANK": "Bank line with no ledger entry and no documented reason: investigate",
    "UNMATCHED_LEDGER": "Ledger entry not on the bank statements within the clearing window: investigate",
}


def match_items(ledger: pd.DataFrame, bank_lines: pd.DataFrame, cal, window: int, id_year: int,
                quarantined: pd.DataFrame | None = None, data_end=None) -> pd.DataFrame:
    L = ledger[["ledger_entry_id", "account_id", "value_date", "amount_minor"]]
    B = bank_lines[["bank_line_id", "account_id", "booking_date", "amount_minor"]]
    pairs = L.merge(B, on=["account_id", "amount_minor"], how="inner")
    lag = np.busday_count(pairs["value_date"].values.astype("datetime64[D]"),
                          pairs["booking_date"].values.astype("datetime64[D]"), holidays=cal.holidays)
    pairs = pairs[(pairs["booking_date"] >= pairs["value_date"]) & (lag <= window)].assign(lag=lag[
        ((pairs["booking_date"] >= pairs["value_date"]) & (lag <= window)).to_numpy()])
    pairs = pairs.sort_values(["value_date", "lag", "ledger_entry_id", "booking_date", "bank_line_id"], kind="mergesort")
    used_l, used_b, rows = set(), set(), []
    for lid, bid, acct, amt, ld, bd in zip(pairs["ledger_entry_id"], pairs["bank_line_id"], pairs["account_id"],
                                           pairs["amount_minor"], pairs["value_date"], pairs["booking_date"]):
        if lid in used_l or bid in used_b:
            continue
        used_l.add(lid)
        used_b.add(bid)
        rows.append((acct, lid, bid, int(amt), ld, bd, "MATCHED" if bd == ld else "TIMING_DIFFERENCE"))
    lo = L[~L["ledger_entry_id"].isin(used_l)]
    in_transit = np.zeros(len(lo), dtype=bool)
    if data_end is not None and len(lo):
        in_transit = pd.to_datetime(cal.add_business_days(lo["value_date"], window)).to_numpy() > np.datetime64(pd.Timestamp(data_end))
    rows += [(a, lid, None, int(m), d, pd.NaT, "LEDGER_ONLY" if ok else "UNMATCHED_LEDGER") for lid, a, d, m, ok in
             zip(lo["ledger_entry_id"], lo["account_id"], lo["value_date"], lo["amount_minor"], in_transit)]
    bo = bank_lines[~bank_lines["bank_line_id"].isin(used_b)]
    q_keys = set()
    if quarantined is not None and len(quarantined):
        qp = quarantined[["account_id", "amount_minor", "value_date"]].merge(
            bo[["bank_line_id", "account_id", "amount_minor", "booking_date"]], on=["account_id", "amount_minor"])
        qlag = np.busday_count(qp["value_date"].values.astype("datetime64[D]"),
                               qp["booking_date"].values.astype("datetime64[D]"), holidays=cal.holidays)
        q_keys = set(qp.loc[(qp["booking_date"] >= qp["value_date"]).to_numpy() & (qlag <= window), "bank_line_id"])
    explained_bank = bo["bank_tx_code"].eq("FEE").to_numpy() | bo["bank_line_id"].isin(q_keys).to_numpy()
    rows += [(a, None, bid, int(m), pd.NaT, d, "BANK_ONLY" if ok else "UNMATCHED_BANK") for bid, a, d, m, ok in
             zip(bo["bank_line_id"], bo["account_id"], bo["booking_date"], bo["amount_minor"], explained_bank)]
    out = pd.DataFrame(rows, columns=["account_id", "ledger_entry_id", "bank_line_id", "amount_minor", "ledger_date",
                                      "bank_date", "match_status"])
    out["explanation"] = out["match_status"].map(EXPLAIN)
    out["_d"] = out["ledger_date"].fillna(out["bank_date"])
    out = out.sort_values(["account_id", "_d", "match_status", "ledger_entry_id", "bank_line_id"], kind="mergesort",
                          na_position="last").drop(columns="_d").reset_index(drop=True)
    out.insert(0, "recon_item_id", [f"REC-{id_year}-{i:08d}" for i in range(1, len(out) + 1)])
    out["amount_minor"] = out["amount_minor"].astype(np.int64)
    return out


def explained_difference(items: pd.DataFrame, index: pd.MultiIndex) -> pd.Series:
    """REC-03 cumulative explained (statement - ledger) difference for every (account_id, position_date)."""
    t = items[items["match_status"] == "TIMING_DIFFERENCE"]
    lo = items[items["match_status"] == "LEDGER_ONLY"]
    bo = items[items["match_status"] == "BANK_ONLY"]
    steps = pd.concat([
        pd.DataFrame({"account_id": t["account_id"], "d": t["ledger_date"], "x": -t["amount_minor"]}),
        pd.DataFrame({"account_id": t["account_id"], "d": t["bank_date"], "x": t["amount_minor"]}),
        pd.DataFrame({"account_id": lo["account_id"], "d": lo["ledger_date"], "x": -lo["amount_minor"]}),
        pd.DataFrame({"account_id": bo["account_id"], "d": bo["bank_date"], "x": bo["amount_minor"]}),
    ], ignore_index=True)
    if steps.empty:
        return pd.Series(0, index=index, dtype=np.int64)
    daily = steps.groupby(["account_id", "d"])["x"].sum()
    daily.index.names = index.names
    return daily.reindex(index, fill_value=0).groupby(level=0).cumsum().astype(np.int64)
