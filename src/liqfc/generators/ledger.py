"""LedgerEntry generation and opening-balance calibration.

Order (LED-* rules):
1. settlement entries from receivable/payable settlement plans;
2. monthly bank fees on the last business day for every active account;
3. sundry receipts/payments fill the remaining count exactly (count is a config target);
4. opening balances derived: buffer(type) + max(0, -min cumulative flow), rounded up to 1,000 units;
5. monthly credit interest derived from the month-end balance (balance x bp x days / 365);
6. posting lag: a share of entries post on the next business day; recorded_at = posting 18:00Z;
7. deterministic sort and LED ids.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids, money
from liqfc.generators.context import GenContext


def build_ledger(ctx: GenContext, accounts: pd.DataFrame, settlements: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    s = ctx.settings
    cfg = s.gen["ledger"]
    acfg = s.gen["account"]
    cal = ctx.cal
    target = int(s.scale["ledger_entries"])
    dormant = ctx.plans.get("dormant_date", pd.Series(dtype="datetime64[ns]"))
    acc = accounts.set_index("account_id")
    month_ends = [pd.Timestamp(d) for d in cal.month_end_business_days(s.start, s.end)]

    def active_on(account_id: str, d: pd.Timestamp) -> bool:
        dd = dormant.get(account_id)
        return dd is None or pd.isna(dd) or d < dd

    settle = settlements.copy()
    settle["description"] = settle["entry_type"].str.replace("_", " ").str.lower() + " " + settle["source_id"]

    rng_f = ctx.rng("ledger:fees")
    fee_rows, interest_slots = [], []
    flo, fhi = acfg["monthly_fee_reporting_range"]
    for aid in accounts["account_id"]:
        for me in month_ends:
            if not active_on(aid, me):
                continue
            usd = rng_f.uniform(flo, fhi)
            amt = int(ctx.usd_to_local_minor([usd], [acc.at[aid, "currency"]], [me])[0])
            fee_rows.append({"account_id": aid, "value_date": me, "currency": acc.at[aid, "currency"],
                             "amount_minor": -max(amt, 1), "entry_type": "BANK_FEE", "source_type": None,
                             "source_id": None, "instalment_no": 0, "counterparty_ref": acc.at[aid, "bank_name"],
                             "description": f"Account maintenance fee {me:%Y-%m}"})
            if acc.at[aid, "annual_interest_bp"] > 0:
                interest_slots.append((aid, me))
    fees = pd.DataFrame(fee_rows)

    n_sundry = target - len(settle) - len(fees) - len(interest_slots)
    if n_sundry < 0:
        raise ValueError(f"ledger target {target} too small: {len(settle)} settlements + {len(fees)} fees + "
                         f"{len(interest_slots)} interest entries")
    sundry = _sundry(ctx, accounts, n_sundry, active_on)

    base = pd.concat([settle, fees, sundry], ignore_index=True)
    base["value_date"] = pd.to_datetime(base["value_date"])

    accounts = accounts.copy()
    opening = {}
    for aid, g in base.groupby("account_id"):
        daily = g.groupby("value_date")["amount_minor"].sum().sort_index().cumsum()
        need = max(0, -int(daily.min()))
        opening[aid] = need
    for i, r in accounts.iterrows():
        S = ctx.entity_size[r["entity_id"]]
        buf_usd = S * float(acfg["opening_buffer_months"][r["account_type"]])
        buf = int(ctx.usd_to_local_minor([buf_usd], [r["currency"]], [s.start - pd.Timedelta(days=1)])[0])
        unit = 1000 * 10 ** ctx.mu[r["currency"]]
        total = buf + opening.get(r["account_id"], 0)
        accounts.at[i, "opening_balance_minor"] = int(-(-total // unit) * unit)

    open_bal = accounts.set_index("account_id")["opening_balance_minor"]
    by_acc = {aid: g for aid, g in base.groupby("account_id")}
    interest_rows = []
    credited: dict[str, int] = {}
    for aid, me in sorted(interest_slots, key=lambda x: (x[1], x[0])):
        g = by_acc.get(aid)
        moved = int(g.loc[g["value_date"] <= me, "amount_minor"].sum()) if g is not None else 0
        bal = int(open_bal[aid]) + moved + credited.get(aid, 0)
        days = me.days_in_month
        amt = money.div_half_even(bal * int(acc.at[aid, "annual_interest_bp"]) * days, 10_000 * 365)
        if amt <= 0:
            raise ValueError(f"non-positive interest for {aid} at {me:%Y-%m}: balance {bal}")
        credited[aid] = credited.get(aid, 0) + amt
        interest_rows.append({"account_id": aid, "value_date": me, "currency": acc.at[aid, "currency"],
                              "amount_minor": amt, "entry_type": "INTEREST", "source_type": None, "source_id": None,
                              "instalment_no": 0, "counterparty_ref": acc.at[aid, "bank_name"],
                              "description": f"Credit interest {me:%Y-%m}"})
    led = pd.concat([base, pd.DataFrame(interest_rows)], ignore_index=True)

    rng_p = ctx.rng("ledger:posting")
    led["x_draw"] = np.arange(len(led))
    led = led.sort_values(["value_date", "account_id", "entry_type", "amount_minor", "x_draw"], kind="mergesort").reset_index(drop=True)
    lag = rng_p.random(len(led)) < cfg["posting_lag_probability"]
    nxt = pd.to_datetime(cal.add_business_days(led["value_date"], 1))
    led["posting_date"] = np.where(lag & (nxt <= s.end), nxt, led["value_date"])
    led["posting_date"] = pd.to_datetime(led["posting_date"])
    led["recorded_at"] = led["posting_date"] + pd.Timedelta(hours=int(cfg["posting_hour_utc"]))

    led["_src"] = led["source_id"].fillna("")
    led = led.sort_values(["value_date", "account_id", "entry_type", "_src", "instalment_no", "amount_minor", "x_draw"],
                          kind="mergesort").reset_index(drop=True)
    led.insert(0, "ledger_entry_id", [ids.ledger_id(s.id_year, i) for i in range(1, len(led) + 1)])
    led["amount_minor"] = led["amount_minor"].astype(np.int64)
    led["instalment_no"] = led["instalment_no"].astype(int)
    return led.drop(columns=["x_draw", "_src"]), accounts


def _sundry(ctx: GenContext, accounts: pd.DataFrame, n: int, active_on) -> pd.DataFrame:
    s = ctx.settings
    cfg = s.gen["ledger"]
    rng = ctx.rng("ledger:sundry")
    w = accounts["account_type"].map(cfg["sundry_account_weights"]).to_numpy(float)
    w *= np.sqrt([ctx.entity_size[e] for e in accounts["entity_id"]])
    pick = rng.choice(len(accounts), size=n, p=w / w.sum())
    bdays = pd.to_datetime(ctx.cal.business_days(s.start, s.end))
    day = bdays[rng.integers(0, len(bdays), n)]
    a = accounts.iloc[pick].reset_index(drop=True)
    for i in range(n):
        if not active_on(a.at[i, "account_id"], day[i]):
            raise ValueError("sundry drawn for dormant account (sundry weight must be 0 for RESERVE)")
    S = np.array([ctx.entity_size[e] for e in a["entity_id"]], dtype=float)
    sig = float(cfg["sundry_sigma"])
    usd = np.minimum(np.exp(rng.normal(np.log(S * cfg["sundry_median_ratio"]), sig)), S * cfg["sundry_cap_ratio"])
    receipt = rng.random(n) < a["account_type"].map(cfg["sundry_receipt_probability"]).to_numpy(float)
    amt = np.maximum(ctx.usd_to_local_minor(usd, a["currency"].to_numpy(), day), 1)
    sign = np.where(receipt, 1, -1)
    return pd.DataFrame({
        "account_id": a["account_id"], "value_date": day, "currency": a["currency"],
        "amount_minor": (sign * amt).astype(np.int64),
        "entry_type": np.where(receipt, "SUNDRY_RECEIPT", "SUNDRY_PAYMENT"),
        "source_type": None, "source_id": None, "instalment_no": 0,
        "counterparty_ref": [f"SUNDRY-{int(x):04d}" for x in rng.integers(1, 5000, n)],
        "description": np.where(receipt, "Sundry receipt", "Sundry payment"),
    })
