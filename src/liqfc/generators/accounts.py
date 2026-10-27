"""BankAccount + AccountStatusEvent generation.

Opening balances are filled later by ledger.calibrate_opening_balances (they are derived
from the account's flows so the clean baseline stays above a documented buffer).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids
from liqfc.core.enums import CURRENCIES
from liqfc.generators.context import GenContext

BANKS = ["Synthetic Bank Alpha", "Synthetic Bank Beta", "Synthetic Bank Gamma", "Synthetic Bank Delta"]
BASE_TYPES = ["OPERATING", "COLLECTION", "DISBURSEMENT"]
TYPE_ORDER = {"OPERATING": 0, "COLLECTION": 1, "DISBURSEMENT": 2, "PAYROLL": 3, "RESERVE": 4}


def _allocate_counts(rng, sizes: np.ndarray, total: int, lo: int, hi: int) -> np.ndarray:
    counts = np.full(len(sizes), lo, dtype=int)
    w = np.sqrt(sizes.astype(float))
    while counts.sum() < total:
        open_ = counts < hi
        if not open_.any():
            raise ValueError("cannot allocate accounts within max_per_entity")
        p = np.where(open_, w, 0.0)
        counts[rng.choice(len(sizes), p=p / p.sum())] += 1
    return counts


def generate_accounts(ctx: GenContext, entities: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    s = ctx.settings
    cfg = s.gen["account"]
    rng = ctx.rng("accounts")
    total = int(s.scale["bank_accounts"])
    n_dormant = int(s.scale["dormant_accounts"])
    sizes = np.array([ctx.entity_size[e] for e in entities["entity_id"]])
    counts = _allocate_counts(rng, sizes, total, cfg["min_per_entity"], cfg["max_per_entity"])

    extra_types = list(cfg["extra_type_weights"])
    extra_p = np.array([cfg["extra_type_weights"][t] for t in extra_types], dtype=float)
    extra_p /= extra_p.sum()

    plan = []
    for (eid, func), k in zip(entities[["entity_id", "functional_currency"]].itertuples(index=False), counts):
        types = list(BASE_TYPES)
        for _ in range(k - len(BASE_TYPES)):
            t = rng.choice(extra_types, p=extra_p)
            if t == "PAYROLL" and "PAYROLL" in types:
                t = "COLLECTION"
            types.append(str(t))
        plan.extend((eid, func, t) for t in types)

    n_reserve = sum(1 for p in plan if p[2] == "RESERVE")
    idx = 0
    seen: dict[str, int] = {}
    for i, (eid, func, t) in enumerate(plan):
        seen[eid] = seen.get(eid, 0) + 1
        if n_reserve >= n_dormant:
            break
        if seen[eid] > len(BASE_TYPES) and t in ("COLLECTION", "DISBURSEMENT"):
            plan[i] = (eid, func, "RESERVE")
            n_reserve += 1
    if n_reserve < n_dormant:
        raise ValueError("not enough extra accounts to host dormant accounts")

    rows = []
    per_entity: dict[str, int] = {}
    for eid, func, t in sorted(plan, key=lambda p: (p[0], TYPE_ORDER[p[2]])):
        per_entity[eid] = per_entity.get(eid, 0) + 1
        if t in ("OPERATING", "PAYROLL") or rng.random() >= cfg["foreign_currency_share"]:
            ccy = func
        else:
            ccy = str(rng.choice([c for c in CURRENCIES if c != func]))
        rows.append({
            "account_id": ids.account_id(eid, per_entity[eid]),
            "entity_id": eid,
            "bank_name": str(rng.choice(BANKS)),
            "account_number_masked": f"****{int(rng.integers(0, 10000)):04d}",
            "currency": ccy,
            "account_type": t,
            "opened_date": pd.Timestamp("2016-01-01") + pd.Timedelta(days=int(rng.integers(0, 3460))),
            "opening_balance_date": s.start,
            "opening_balance_minor": 0,
            "annual_interest_bp": int(cfg["annual_interest_bp"][t]),
            "status": "ACTIVE",
        })
    acc = pd.DataFrame(rows)

    for ccy in CURRENCIES:
        if not (acc["currency"] == ccy).any():
            cand = acc.index[acc["account_type"].isin(["COLLECTION", "DISBURSEMENT", "RESERVE"])
                             & acc.duplicated("currency", keep="first")]
            acc.loc[cand[0], "currency"] = ccy

    reserve = acc.index[acc["account_type"] == "RESERVE"].to_numpy()
    dormant_idx = np.sort(rng.choice(reserve, size=n_dormant, replace=False)) if n_dormant else []
    lo, hi = cfg["dormant_window"]
    bdays = ctx.cal.business_days(lo, hi)
    dormant_date = {int(i): pd.Timestamp(bdays[int(rng.integers(0, len(bdays)))]) for i in dormant_idx}

    events = []
    for i, r in acc.iterrows():
        events.append((r["account_id"], None, "ACTIVE", r["opened_date"], "Account opened"))
        if i in dormant_date:
            events.append((r["account_id"], "ACTIVE", "DORMANT", dormant_date[i], "No activity expected; account set dormant"))
            acc.loc[i, "status"] = "DORMANT"
    ev = pd.DataFrame(events, columns=["account_id", "from_status", "to_status", "event_date", "reason"])
    ev = ev.sort_values(["event_date", "account_id"], kind="mergesort").reset_index(drop=True)
    ev.insert(0, "event_id", ids.seq_ids("ASE", s.id_year, len(ev)))
    ev["recorded_at"] = ev["event_date"] + pd.Timedelta(hours=9)
    ctx.plans["dormant_date"] = pd.Series({acc.loc[i, "account_id"]: d for i, d in dormant_date.items()}, dtype="datetime64[ns]")
    return acc, ev[["event_id", "account_id", "from_status", "to_status", "event_date", "recorded_at", "reason"]]
