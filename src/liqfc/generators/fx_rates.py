"""CurrencyRate generation: pinned external rates (hybrid) or a business-day geometric random walk
per currency + configured shocks (synthetic; fx.synthetic_walk)."""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from liqfc.core import ids, money
from liqfc.generators.context import GenContext


def generate_rates(ctx: GenContext) -> pd.DataFrame:
    s = ctx.settings
    first = ctx.cal.roll_backward([s.start - pd.Timedelta(days=1)])[0]
    bdays = ctx.cal.business_days(first, s.end)
    if s.external["fx_source"] == "kaggle":
        return load_external_rates(ctx, bdays)
    return _synthetic_rates(ctx, bdays)


def fx_source_note(settings) -> str:
    """One-line statement of where rates come from and which configured FX settings are ignored."""
    walk = settings.raw["fx"]["synthetic_walk"]
    if settings.external["fx_source"] == "kaggle":
        return (f"FX rates: pinned external file {settings.external['fx_file']} (real market moves); "
                f"fx.synthetic_walk ignored ({len(walk.get('shocks', []))} configured shocks not applied)")
    shocks = ", ".join(f"{x['currency']} {x['pct']}% on {x['date']}" for x in walk.get("shocks", [])) or "none"
    return f"FX rates: synthetic random walk with configured shocks ({shocks})"


def load_external_rates(ctx: GenContext, bdays) -> pd.DataFrame:
    """Pinned real rates (USD per unit) from data/external; must cover every required business day."""
    s = ctx.settings
    rep = s.reporting_currency
    ext = pd.read_csv(s.external_path("fx_file"), dtype=str, keep_default_na=False)
    ext["rate_date"] = pd.to_datetime(ext["rate_date"])
    need = sorted(s.raw["fx"]["initial_rates"])
    dates = pd.to_datetime(bdays)
    ext = ext[ext["rate_date"].isin(dates) & ext["base_currency"].isin(need)]
    have = set(zip(ext["base_currency"], ext["rate_date"]))
    gaps = [(c, d) for c in need for d in dates if (c, d) not in have]
    if gaps or (ext["quote_currency"] != rep).any():
        raise ValueError(f"external FX file incomplete or not quoted in {rep}: {gaps[:5]}")
    for r in ext["rate"]:
        money.parse_rate(r)
    return pd.DataFrame({
        "rate_id": [ids.rate_id(d, c, rep) for d, c in zip(ext["rate_date"], ext["base_currency"])],
        "rate_date": ext["rate_date"].values, "base_currency": ext["base_currency"].values,
        "quote_currency": rep, "rate": ext["rate"].values, "source": "EXTERNAL_KAGGLE_FOREX",
        "published_at": (ext["rate_date"] + pd.Timedelta(hours=16)).values,
    }).sort_values("rate_id").reset_index(drop=True)


def _synthetic_rates(ctx: GenContext, bdays) -> pd.DataFrame:
    s = ctx.settings
    fx = s.raw["fx"]
    walk = fx["synthetic_walk"]
    rep = s.reporting_currency
    dt = 1.0 / 252.0
    frames = []
    for ccy in sorted(fx["initial_rates"]):
        rng = ctx.rng(f"fx:{ccy}")
        sigma = float(walk["annual_volatility"][ccy])
        steps = rng.normal(-0.5 * sigma**2 * dt, sigma * np.sqrt(dt), len(bdays))
        steps[0] = 0.0
        for shock in walk.get("shocks", []):
            if shock["currency"] == ccy:
                pos = np.searchsorted(bdays, np.datetime64(shock["date"]))
                if pos < len(bdays):
                    steps[pos] += np.log1p(float(Decimal(shock["pct"]) / 100))
        path = float(Decimal(fx["initial_rates"][ccy])) * np.exp(np.cumsum(steps))
        rate_e8 = np.round(path * money.RATE_SCALE).astype(np.int64)
        dates = pd.to_datetime(bdays)
        frames.append(pd.DataFrame({
            "rate_id": [ids.rate_id(d, ccy, rep) for d in dates],
            "rate_date": dates,
            "base_currency": ccy,
            "quote_currency": rep,
            "rate": [money.format_rate(int(r)) for r in rate_e8],
            "source": "SYNTHETIC_GBM",
            "published_at": dates + pd.Timedelta(hours=16),
        }))
    return pd.concat(frames, ignore_index=True).sort_values("rate_id").reset_index(drop=True)
