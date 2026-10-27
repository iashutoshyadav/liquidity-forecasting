"""Scenario definitions and the documented scenario transformation.

A scenario may change a forecast ONLY through these assumptions (ASM-<CODE>-NN):
  01 receivable_delay_proportion  02 receivable_delay_days  03 collection_haircut_bp
  04 fx_shock_bp                  05 one_off_payment
Delay and haircut apply to CUSTOMER_RECEIPT lines only (intercompany and payables unchanged).
Selection is a stable hash: sha256(seed|scenario_code|cf_id)/2^64 < proportion.
"""
from __future__ import annotations

import json
from decimal import Decimal

import numpy as np
import pandas as pd

from liqfc.core import money
from liqfc.core.config import Settings, canonical_json, sha256_text
from liqfc.core.rng import stable_unit

DELAYABLE = "CUSTOMER_RECEIPT"


def asm(code: str, n: int) -> str:
    return f"ASM-{code}-{n:02d}"


def build_scenario_tables(settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    scen, assumptions = [], []
    for code, p in settings.scenarios.items():
        scen.append({
            "scenario_id": p["scenario_id"], "scenario_code": code, "scenario_name": p["name"],
            "description": p["description"], "version": 1, "status": "ACTIVE",
            "effective_from": settings.start, "parameter_hash": sha256_text(canonical_json(p)),
        })
        rd = p["receivable_delay"]
        rows = [
            ("receivable_delay_proportion", str(rd["proportion"]), "fraction of CUSTOMER_RECEIPT lines",
             "Share of customer receipts delayed (stable hash selection)"),
            ("receivable_delay_days", str(rd["delay_days"]), "calendar days",
             "Delay added to selected receipts, then rolled to next business day"),
            ("collection_haircut_bp", str(p["collection_haircut_bp"]), "basis points",
             "Reduction applied to every CUSTOMER_RECEIPT line amount (half-even)"),
            ("fx_shock_bp", json.dumps(p["fx_shock_bp"] or {}, sort_keys=True), "basis points per currency",
             "Shock applied to the cutoff rate used to convert to reporting currency"),
            ("one_off_payment", json.dumps(p["one_off_payment"], sort_keys=True) if p["one_off_payment"] else "none",
             "reporting minor units", "Additional hypothetical outflow (not in source data)"),
        ]
        for i, (param, value, unit, desc) in enumerate(rows, start=1):
            assumptions.append({"assumption_id": asm(code, i), "scenario_id": p["scenario_id"], "parameter": param,
                                "value": value, "unit": unit, "description": desc})
    return pd.DataFrame(scen), pd.DataFrame(assumptions)


def delayed_mask(seed: int, code: str, cf_ids, categories, proportion) -> np.ndarray:
    prop = float(Decimal(str(proportion)))
    if prop <= 0:
        return np.zeros(len(cf_ids), dtype=bool)
    return np.array([c == DELAYABLE and stable_unit(seed, code, i) < prop for i, c in zip(cf_ids, categories)])


def apply_scenario(lines: pd.DataFrame, code: str, settings: Settings, cal) -> pd.DataFrame:
    """Transform BASE candidate lines (base_date/base_amount_minor) into scenario-adjusted lines."""
    p = settings.scenarios[code]
    out = lines.copy()
    out["adjusted_date"] = out["base_date"]
    out["adjusted_amount_minor"] = out["base_amount_minor"].astype(np.int64)
    tags = [[] for _ in range(len(out))]
    if len(out):
        rd = p["receivable_delay"]
        mask = delayed_mask(settings.seed, code, out["cf_id"].tolist(), out["flow_category"].tolist(), rd["proportion"])
        if mask.any() and int(rd["delay_days"]) > 0:
            shifted = pd.to_datetime(cal.roll_forward(out.loc[mask, "base_date"] + pd.Timedelta(days=int(rd["delay_days"]))))
            out.loc[mask, "adjusted_date"] = shifted.values
            for i in np.where(mask)[0]:
                tags[i] += [asm(code, 1), asm(code, 2)]
        h = int(p["collection_haircut_bp"])
        if h:
            hm = (out["flow_category"] == DELAYABLE).to_numpy()
            out.loc[hm, "adjusted_amount_minor"] = [money.apply_bp(a, money.BP_SCALE - h)
                                                    for a in out.loc[hm, "adjusted_amount_minor"]]
            for i in np.where(hm)[0]:
                tags[i].append(asm(code, 3))
    out["assumption_ids"] = [";".join(t) if t else None for t in tags]
    out["adjusted_date"] = pd.to_datetime(out["adjusted_date"])
    return out


def fx_shock_bp(settings: Settings, code: str, ccy: str) -> int:
    return int((settings.scenarios[code]["fx_shock_bp"] or {}).get(ccy, 0))


def one_off_line(settings: Settings, code: str, cutoff: pd.Timestamp, accounts: pd.DataFrame, fx, cal) -> dict | None:
    p = settings.scenarios[code]["one_off_payment"]
    if not p:
        return None
    acc = accounts[(accounts["entity_id"] == p["entity_id"]) & (accounts["account_type"] == p["account_type"])]
    if acc.empty:
        raise ValueError(f"scenario {code}: no {p['account_type']} account for {p['entity_id']}")
    a = acc.sort_values("account_id").iloc[0]
    rate, _, status = fx.effective(a["currency"], cutoff - pd.Timedelta(days=1))
    if rate is None:
        raise ValueError(f"scenario {code}: no FX rate to size one-off payment")
    mu = settings.minor_units
    local = money.convert_inverse(int(p["amount_reporting_minor"]), mu[settings.reporting_currency], rate, mu[a["currency"]])
    d = pd.Timestamp(cal.roll_forward([cutoff + pd.Timedelta(days=int(p["offset_days"]))])[0])
    return {"cf_id": None, "account_id": a["account_id"], "source_type": "SCENARIO", "source_id": asm(code, 5),
            "currency": a["currency"], "flow_category": "ONE_OFF_PAYMENT", "base_date": d, "base_amount_minor": -local,
            "adjusted_date": d, "adjusted_amount_minor": -local, "settled_before_cutoff_minor": 0,
            "assumption_ids": asm(code, 5), "basis_record_id": asm(code, 5),
            "basis_recorded_at": cutoff - pd.Timedelta(seconds=1), "line_status": "INCLUDED", "exclusion_reason": None,
            "known_at": cutoff - pd.Timedelta(seconds=1)}
