"""LedgerEntry generation, cash pooling and opening-balance calibration.

Order (LED-* rules):
1. settlement entries from receivable/payable settlement plans;
2. monthly bank fees on the last business day for every active account, plus a per-sweep bank fee
   on every pooled account on each sweep day (so a quiet week still has a non-zero sweep);
3. sundry receipts/payments fill the remaining count exactly (count is a config target); small
   tickets with a weekday and month-end activity profile;
4. cash pool (LED-07): pooled accounts (sweep_parent_account_id set) are swept back to their
   target balance on the last business day of every week; the difference moves to/from the
   entity's OPERATING account as an INTERNAL_TRANSFER_OUT / INTERNAL_TRANSFER_IN pair;
5. opening balances derived: pooled accounts open at their target = buffer + largest intra-week
   drawdown; other accounts at buffer + max(0, -min cumulative flow incl. sweeps), rounded up to
   1,000 units;
6. monthly credit interest from the month-end balance (balance x bp x days / 365), credited before
   that day's sweep;
7. posting lag: a share of non-sweep entries post on the next business day; recorded_at = 18:00Z;
8. deterministic sort and LED ids.
The number of sweep entries (pooled accounts x sweep weeks x 2) and sweep fees (x 1) is fixed in
advance, so the ledger always has exactly the configured number of entries.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids, money
from liqfc.generators.context import GenContext


def _round_up(value: int, unit: int) -> int:
    return int(-(-value // unit) * unit)


def build_ledger(ctx: GenContext, accounts: pd.DataFrame, settlements: pd.DataFrame
                 ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (ledger, accounts with opening balances, economic cash events).

    The cash events are the underlying transactions before any ledger-specific treatment (posting
    lag, ledger ids). The bank-side model builds its own statement lines from them.
    """
    s = ctx.settings
    cfg = s.gen["ledger"]
    acfg = s.gen["account"]
    cal = ctx.cal
    target = int(s.scale["ledger_entries"])
    dormant = ctx.plans.get("dormant_date", pd.Series(dtype="datetime64[ns]"))
    acc = accounts.set_index("account_id")
    month_ends = [pd.Timestamp(d) for d in cal.month_end_business_days(s.start, s.end)]
    sweep_dates = cal.weekly_sweep_dates(s.start, s.end)
    pooled = accounts.loc[accounts["sweep_parent_account_id"].notna(), "account_id"].tolist()

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
    rng_sf = ctx.rng("ledger:sweep_fees")
    slo, shi = acfg["cash_pool"]["sweep_fee_reporting_range"]
    for aid in sorted(pooled):
        for d in sweep_dates:
            usd = rng_sf.uniform(slo, shi)
            amt = int(ctx.usd_to_local_minor([usd], [acc.at[aid, "currency"]], [d])[0])
            fee_rows.append({"account_id": aid, "value_date": d, "currency": acc.at[aid, "currency"],
                             "amount_minor": -max(amt, 1), "entry_type": "BANK_FEE", "source_type": None,
                             "source_id": None, "instalment_no": 0, "counterparty_ref": acc.at[aid, "bank_name"],
                             "description": f"Cash pool sweep fee {d:%Y-%m-%d}"})
    fees = pd.DataFrame(fee_rows)

    n_sweep_entries = 2 * len(pooled) * len(sweep_dates)
    n_sundry = target - len(settle) - len(fees) - len(interest_slots) - n_sweep_entries
    if n_sundry < 0:
        raise ValueError(f"ledger target {target} too small: {len(settle)} settlements + {len(fees)} fees + "
                         f"{len(interest_slots)} interest + {n_sweep_entries} sweep entries")
    sundry = _sundry(ctx, accounts, n_sundry, active_on)

    base = pd.concat([settle, fees, sundry], ignore_index=True)
    base["value_date"] = pd.to_datetime(base["value_date"])
    days = pd.date_range(s.start, s.end, freq="D")
    net = base.groupby(["account_id", "value_date"])["amount_minor"].sum()
    flows = {aid: net.xs(aid).reindex(days, fill_value=0).astype(np.int64)
             if aid in net.index.get_level_values(0) else pd.Series(0, index=days, dtype=np.int64)
             for aid in accounts["account_id"]}

    accounts = accounts.copy()
    unit = {aid: 1000 * 10 ** ctx.mu[acc.at[aid, "currency"]] for aid in accounts["account_id"]}

    def buffer(aid: str, months: float) -> int:
        usd = ctx.entity_size[acc.at[aid, "entity_id"]] * months
        return int(ctx.usd_to_local_minor([usd], [acc.at[aid, "currency"]], [s.start - pd.Timedelta(days=1)])[0])

    sweep_set = set(sweep_dates)
    pool_target: dict[str, int] = {}
    pass_a_sweeps = {aid: pd.Series(0, index=days, dtype=np.int64) for aid in accounts["account_id"]}
    for aid in pooled:
        f = flows[aid]
        segment = (f.index.isin(sweep_dates)).cumsum()
        segment = np.r_[0, segment[:-1]]
        drawdown = int((-f.groupby(segment).cumsum()).max())
        pool_target[aid] = _round_up(buffer(aid, float(acfg["cash_pool"]["target_buffer_months"])) + max(0, drawdown), unit[aid])
        seg_net = f.groupby(segment).sum()
        for k, d in enumerate(sweep_dates):
            x = int(seg_net.get(k, 0))
            parent = acc.at[aid, "sweep_parent_account_id"]
            pass_a_sweeps[aid][d] -= x
            pass_a_sweeps[parent][d] += x

    opening = {}
    for aid in accounts["account_id"]:
        if aid in pool_target:
            opening[aid] = pool_target[aid]
            continue
        path = (flows[aid] + pass_a_sweeps[aid]).cumsum()
        need = max(0, -int(path.min()))
        months = float(acfg["opening_buffer_months"][acc.at[aid, "account_type"]])
        opening[aid] = _round_up(buffer(aid, months) + need, unit[aid])
    accounts["opening_balance_minor"] = accounts["account_id"].map(opening).astype(np.int64)
    accounts["sweep_target_minor"] = pd.array([pool_target.get(a) for a in accounts["account_id"]], dtype="Int64")

    slots_by_day: dict[pd.Timestamp, list[str]] = {}
    for aid, me in interest_slots:
        slots_by_day.setdefault(me, []).append(aid)
    bal = dict(opening)
    interest_rows, sweep_rows = [], []
    for d in days:
        for aid in bal:
            bal[aid] += int(flows[aid][d])
        for aid in sorted(slots_by_day.get(d, [])):
            amt = money.div_half_even(bal[aid] * int(acc.at[aid, "annual_interest_bp"]) * d.days_in_month, 10_000 * 365)
            if amt <= 0:
                raise ValueError(f"non-positive interest for {aid} at {d:%Y-%m}: balance {bal[aid]}")
            bal[aid] += amt
            interest_rows.append({"account_id": aid, "value_date": d, "currency": acc.at[aid, "currency"],
                                  "amount_minor": amt, "entry_type": "INTEREST", "source_type": None, "source_id": None,
                                  "instalment_no": 0, "counterparty_ref": acc.at[aid, "bank_name"],
                                  "description": f"Credit interest {d:%Y-%m}"})
        if d in sweep_set:
            for aid in sorted(pooled):
                parent = acc.at[aid, "sweep_parent_account_id"]
                x = bal[aid] - pool_target[aid]
                if x == 0:
                    raise ValueError(f"zero cash-pool sweep for {aid} on {d:%Y-%m-%d}")
                bal[aid] -= x
                bal[parent] += x
                out_type, in_type = ("INTERNAL_TRANSFER_OUT", "INTERNAL_TRANSFER_IN") if x > 0 else \
                    ("INTERNAL_TRANSFER_IN", "INTERNAL_TRANSFER_OUT")
                ccy = acc.at[aid, "currency"]
                sweep_rows.append({"account_id": aid, "value_date": d, "currency": ccy, "amount_minor": -x,
                                   "entry_type": out_type, "source_type": None, "source_id": None, "instalment_no": 0,
                                   "counterparty_ref": parent, "description": f"Cash pool sweep to target {d:%Y-%m-%d}"})
                sweep_rows.append({"account_id": parent, "value_date": d, "currency": ccy, "amount_minor": x,
                                   "entry_type": in_type, "source_type": None, "source_id": None, "instalment_no": 0,
                                   "counterparty_ref": aid, "description": f"Cash pool sweep from {aid} {d:%Y-%m-%d}"})
    if min(bal.values()) < 0:
        raise ValueError("calibration failed: negative closing balance")
    led = pd.concat([base, pd.DataFrame(interest_rows), pd.DataFrame(sweep_rows)], ignore_index=True)

    rng_p = ctx.rng("ledger:posting")
    led["x_draw"] = np.arange(len(led))
    led = led.sort_values(["value_date", "account_id", "entry_type", "amount_minor", "x_draw"], kind="mergesort").reset_index(drop=True)
    events = led[["account_id", "value_date", "currency", "amount_minor", "entry_type", "counterparty_ref",
                  "description"]].copy().reset_index(drop=True)
    lag = (rng_p.random(len(led)) < cfg["posting_lag_probability"]) & ~led["entry_type"].str.startswith("INTERNAL_TRANSFER").to_numpy()
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
    if len(led) != target:
        raise ValueError(f"ledger has {len(led)} entries, expected {target}")
    return led.drop(columns=["x_draw", "_src"]), accounts, events


def _sundry(ctx: GenContext, accounts: pd.DataFrame, n: int, active_on) -> pd.DataFrame:
    s = ctx.settings
    cfg = s.gen["ledger"]
    rng = ctx.rng("ledger:sundry")
    w = accounts["account_type"].map(cfg["sundry_account_weights"]).to_numpy(float)
    w = w * np.sqrt([ctx.entity_size[e] for e in accounts["entity_id"]])
    pick = rng.choice(len(accounts), size=n, p=w / w.sum())
    bdays = pd.to_datetime(ctx.cal.business_days(s.start, s.end))
    month_end = set()
    for me in ctx.cal.month_end_business_days(s.start, s.end):
        month_end |= {pd.Timestamp(me), pd.Timestamp(ctx.cal.add_business_days([me], -1)[0])}
    dw = np.array([cfg["sundry_weekday_weights"][d.day_of_week] for d in bdays], dtype=float)
    dw *= np.where(bdays.isin(list(month_end)), float(cfg["sundry_month_end_multiplier"]), 1.0)
    day = bdays[rng.choice(len(bdays), size=n, p=dw / dw.sum())]
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
