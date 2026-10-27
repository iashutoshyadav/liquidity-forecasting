"""Shared generation context: settings, calendar, named RNG streams and hidden profiles.

Hidden profiles (entity revenue scale, settlement plans) drive generation but are never
exported as columns; everything exported is derived from them by documented rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from liqfc.core import money
from liqfc.core.calendar import BusinessCalendar
from liqfc.core.config import Settings
from liqfc.core.rng import stream
from liqfc.engine.fx_conversion import FxLookup


@dataclass
class GenContext:
    settings: Settings
    cal: BusinessCalendar
    entity_size: dict[str, int] = field(default_factory=dict)
    fx: FxLookup | None = None
    plans: dict[str, pd.DataFrame] = field(default_factory=dict)

    @classmethod
    def create(cls, settings: Settings) -> "GenContext":
        return cls(settings=settings, cal=BusinessCalendar(settings.raw["calendar"]["holidays"]))

    def rng(self, name: str) -> np.random.Generator:
        return stream(self.settings.seed, name)

    @property
    def mu(self) -> dict[str, int]:
        return self.settings.minor_units

    def reference_rate(self, ccy: str, date) -> int:
        """Rate used to express generated USD amounts in local currency (FX-07)."""
        if ccy == self.settings.reporting_currency:
            return money.RATE_SCALE
        if self.fx is None:
            return money.parse_rate(self.settings.raw["fx"]["initial_rates"][ccy])
        if pd.Timestamp(date) < self.settings.start:
            date = self.settings.start - pd.Timedelta(days=1)
        rate, _, status = self.fx.effective(ccy, date)
        if rate is None:
            raise ValueError(f"no reference rate for {ccy} on {date}")
        return rate

    def usd_to_local_minor(self, usd_amounts, currencies, dates) -> np.ndarray:
        """Quantize sampled USD amounts to cents, then convert to local minor units (exact ints)."""
        rep = self.settings.reporting_currency
        out = []
        for a, c, d in zip(usd_amounts, currencies, dates):
            usd_minor = int(round(float(a) * 100))
            if c == rep:
                out.append(usd_minor)
            else:
                out.append(money.convert_inverse(usd_minor, self.mu[rep], self.reference_rate(c, d), self.mu[c]))
        return np.array(out, dtype=np.int64)
