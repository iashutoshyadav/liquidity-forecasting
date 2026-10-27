"""LegalEntity generation (master data, generated first)."""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from liqfc.core import ids
from liqfc.core.enums import COUNTRY_CURRENCY
from liqfc.generators.context import GenContext


def generate_entities(ctx: GenContext) -> pd.DataFrame:
    s = ctx.settings
    cfg = s.gen["entity"]
    n = int(s.scale["legal_entities"])
    rng = ctx.rng("entities")

    countries = list(cfg["country_weights"])
    weights = np.array([cfg["country_weights"][c] for c in countries], dtype=float)
    weights /= weights.sum()
    seed_countries = ["US", "DE", "GB", "IN", "JP"][: min(n, 5)]
    drawn = list(rng.choice(countries, size=max(n - len(seed_countries), 0), p=weights))
    country = seed_countries + drawn

    size = np.exp(rng.normal(np.log(cfg["monthly_revenue_median_reporting"]), cfg["monthly_revenue_sigma"], n))
    size = np.clip(size, cfg["monthly_revenue_min"], cfg["monthly_revenue_max"]).round(0).astype(np.int64)
    eff_days = rng.integers(0, 3650, n)
    policy = Decimal(str(s.forecast["liquidity_policy_months"]))

    rows = []
    for i in range(n):
        eid = ids.entity_id(i + 1)
        ctx.entity_size[eid] = int(size[i])
        band = "SMALL" if size[i] < 1_000_000 else ("MEDIUM" if size[i] < 4_000_000 else "LARGE")
        rows.append({
            "entity_id": eid,
            "entity_name": f"LFX Synthetic Entity {i + 1:03d} ({country[i]})",
            "country_code": country[i],
            "functional_currency": COUNTRY_CURRENCY[country[i]],
            "parent_entity_id": None if i == 0 else ids.entity_id(1),
            "size_band": band,
            "min_liquidity_reporting_minor": int(Decimal(int(size[i])) * policy * 100),
            "status": "ACTIVE",
            "effective_from": pd.Timestamp("2015-01-01") + pd.Timedelta(days=int(eff_days[i])) if i else pd.Timestamp("2015-01-01"),
            "effective_to": pd.NaT,
        })
    return pd.DataFrame(rows)
