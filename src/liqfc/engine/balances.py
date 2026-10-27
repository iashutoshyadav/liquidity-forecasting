"""Daily cash positions: account -> entity -> group.

BAL-01 closing(d) = opening(d) + credits(d) - debits(d); opening(d) = closing(d-1);
       opening(start) = bank_account.opening_balance_minor. Value date drives balances.
BAL-02 entries must be in the account currency; entries that are not are quarantined (excluded from
       balances, forecasts and variance, never summed across currencies) and raised as CCY-001.
BAL-03 account closing converted to reporting currency with the effective rate for d;
       entity/group = sum of converted account balances (unconverted accounts excluded and counted).
BAL-04 calculated closing is reconciled to the independent bank statement closing for the same
       account/date (engine/bank_reconciliation.py): a difference fully explained by open reconciling
       items (timing, bank-only, ledger-only, incl. quarantined entries) is RECONCILING_ITEM; any other
       difference is a BREAK.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core.dataset import Dataset
from liqfc.engine.bank_reconciliation import explained_difference, match_items
from liqfc.engine.fx_conversion import FxLookup, convert_to_reporting, rate_text


def split_ledger_by_currency(ledger: pd.DataFrame, accounts: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(entries in their account currency, quarantined entries in any other currency)."""
    acc_ccy = accounts.set_index("account_id")["currency"]
    ok = ledger["currency"].values == ledger["account_id"].map(acc_ccy).values
    return ledger[ok], ledger[~ok]


def account_daily_balances(accounts: pd.DataFrame, ledger: pd.DataFrame, start, end) -> pd.DataFrame:
    acc_ccy = accounts.set_index("account_id")["currency"]
    ledger, _ = split_ledger_by_currency(ledger, accounts)
    days = pd.date_range(start, end, freq="D")
    grid = pd.MultiIndex.from_product([sorted(accounts["account_id"]), days], names=["account_id", "position_date"])
    amt = ledger["amount_minor"].astype(np.int64)
    agg = pd.DataFrame({
        "account_id": ledger["account_id"], "position_date": ledger["value_date"],
        "credits_minor": amt.clip(lower=0), "debits_minor": (-amt).clip(lower=0), "entry_count": 1,
    }).groupby(["account_id", "position_date"]).sum()
    out = agg.reindex(grid, fill_value=0).reset_index()
    out[["credits_minor", "debits_minor", "entry_count"]] = out[["credits_minor", "debits_minor", "entry_count"]].astype(np.int64)
    net = out["credits_minor"] - out["debits_minor"]
    opening0 = out["account_id"].map(accounts.set_index("account_id")["opening_balance_minor"]).astype(np.int64)
    out["closing_balance_minor"] = opening0 + net.groupby(out["account_id"]).cumsum()
    out["opening_balance_minor"] = out["closing_balance_minor"] - net
    out["currency"] = out["account_id"].map(acc_ccy)
    out["entity_id"] = out["account_id"].map(accounts.set_index("account_id")["entity_id"])
    return out


def daily_cash_positions(ds: Dataset, settings, fx: FxLookup) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily positions with bank reconciliation; returns (positions, bank_reconciliation_item rows)."""
    pos = account_daily_balances(ds["bank_account"], ds["ledger_entry"], settings.start, settings.end)
    rates = fx.frame(pos.rename(columns={"position_date": "date"}), "currency", "date")
    pos = pos.merge(rates.rename(columns={"date": "position_date"}), on=["currency", "position_date"], how="left")
    pos["fx_rate"] = [rate_text(r) for r in pos["rate_e8"]]
    pos["fx_rate_date"] = pos["rate_date"]
    pos["fx_rate_status"] = pos["rate_status"]
    pos["closing_balance_reporting_minor"] = pd.array(
        convert_to_reporting(pos["closing_balance_minor"], pos["currency"], pos["rate_e8"], settings.minor_units,
                             settings.reporting_currency), dtype="Int64")
    st = ds["account_balance_statement"][["account_id", "statement_date", "closing_balance_minor"]].rename(
        columns={"statement_date": "position_date", "closing_balance_minor": "statement_closing_minor"})
    pos = pos.merge(st, on=["account_id", "position_date"], how="left")
    pos["statement_closing_minor"] = pos["statement_closing_minor"].astype("Int64")
    pos["reconciliation_difference_minor"] = (pos["statement_closing_minor"] - pos["closing_balance_minor"]).astype("Int64")
    valid, quarantined = split_ledger_by_currency(ds["ledger_entry"], ds["bank_account"])
    items = match_items(valid, ds["bank_statement_line"], fx.cal,
                        int(settings.raw["reconciliation"]["match_window_business_days"]), settings.id_year,
                        quarantined=quarantined, data_end=settings.end)
    pos = pos.sort_values(["account_id", "position_date"], kind="mergesort").reset_index(drop=True)
    expl = explained_difference(items, pd.MultiIndex.from_frame(pos[["account_id", "position_date"]]))
    pos["explained_difference_minor"] = pd.array(expl.to_numpy(), dtype="Int64")
    diff = pos["reconciliation_difference_minor"]
    known = diff.notna().to_numpy()
    d = diff.fillna(0).astype(np.int64).to_numpy()
    e = expl.to_numpy()
    pos["reconciliation_status"] = np.where(known & (d == 0), "RECONCILED",
                                            np.where(known & (d == e), "RECONCILING_ITEM", "BREAK"))
    return pos, items


def consolidated_positions(positions: pd.DataFrame, entities: pd.DataFrame, reporting: str) -> pd.DataFrame:
    p = positions.copy()
    p["converted"] = p["closing_balance_reporting_minor"].notna()
    p["fallback"] = p["fx_rate_status"].eq("STALE_FALLBACK")
    p["rep"] = p["closing_balance_reporting_minor"].fillna(0).astype(np.int64)
    ent = p.groupby(["entity_id", "position_date"]).agg(
        closing_balance_reporting_minor=("rep", "sum"), account_count=("account_id", "size"),
        accounts_converted=("converted", "sum"), accounts_with_fallback_rate=("fallback", "sum")).reset_index()
    ent["level"] = "ENTITY"
    ent = ent.rename(columns={"entity_id": "scope_id"})
    ent["min_liquidity_reporting_minor"] = ent["scope_id"].map(entities.set_index("entity_id")["min_liquidity_reporting_minor"])
    grp = ent.groupby("position_date").agg(
        closing_balance_reporting_minor=("closing_balance_reporting_minor", "sum"), account_count=("account_count", "sum"),
        accounts_converted=("accounts_converted", "sum"), accounts_with_fallback_rate=("accounts_with_fallback_rate", "sum"),
        min_liquidity_reporting_minor=("min_liquidity_reporting_minor", "sum")).reset_index()
    grp["level"] = "GROUP"
    grp["scope_id"] = "GROUP"
    out = pd.concat([ent, grp], ignore_index=True)
    out["reporting_currency"] = reporting
    out["below_threshold"] = out["closing_balance_reporting_minor"] < out["min_liquidity_reporting_minor"]
    for c in ["closing_balance_reporting_minor", "account_count", "accounts_converted", "accounts_with_fallback_rate",
              "min_liquidity_reporting_minor"]:
        out[c] = out[c].astype(np.int64)
    return out
