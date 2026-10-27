"""Effective-dated FX lookup and conversion to the reporting currency.

Conventions (see docs/business_rules.md, FX-*):
* Quotation: rate = units of reporting currency per 1 unit of base currency.
* Rates exist for business days only (published 16:00Z on rate_date).
* Business day d: use the rate for d (EXACT). Non-business day: use the latest prior
  business-day rate (PRIOR_BUSINESS_DAY).
* If the required business-day rate is missing, fall back to the most recent earlier rate
  within ``max_fallback_business_days`` (STALE_FALLBACK, raised as FX_RATE_MISSING and held
  for review); beyond that the conversion is MISSING and the amount is left unconverted.
* A rate of 1 is never substituted; same-currency amounts are not converted (IDENTITY).
* Rounding: half-even to reporting minor units on each converted amount.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import money
from liqfc.core.calendar import BusinessCalendar


class FxLookup:
    def __init__(self, rates: pd.DataFrame, cal: BusinessCalendar, reporting: str, max_fallback: int = 5):
        self.cal = cal
        self.reporting = reporting
        self.max_fallback = int(max_fallback)
        self.table: dict[tuple[str, np.datetime64], int] = {}
        if len(rates):
            dates = rates["rate_date"].values.astype("datetime64[D]")
            for ccy, d, r in zip(rates["base_currency"], dates, rates["rate"]):
                self.table[(ccy, d)] = money.parse_rate(r)
        self._cache: dict = {}

    def effective(self, ccy: str, date) -> tuple[int | None, np.datetime64 | None, str]:
        if ccy == self.reporting:
            return money.RATE_SCALE, None, "IDENTITY"
        d = np.datetime64(pd.Timestamp(date).date(), "D")
        key = (ccy, d)
        if key in self._cache:
            return self._cache[key]
        if self.cal.is_business_day([d])[0]:
            if key in self.table:
                res = (self.table[key], d, "EXACT")
            else:
                res = self._fallback(ccy, d)
        else:
            prev = self.cal.roll_backward([d])[0]
            if (ccy, prev) in self.table:
                res = (self.table[(ccy, prev)], prev, "PRIOR_BUSINESS_DAY")
            else:
                res = self._fallback(ccy, prev)
        self._cache[key] = res
        return res

    def _fallback(self, ccy: str, d: np.datetime64):
        for k in range(1, self.max_fallback + 1):
            prev = self.cal.add_business_days([d], -k)[0]
            if (ccy, prev) in self.table:
                return self.table[(ccy, prev)], prev, "STALE_FALLBACK"
        return None, None, "MISSING"

    def frame(self, pairs: pd.DataFrame, ccy_col: str = "currency", date_col: str = "date") -> pd.DataFrame:
        """Rate columns (rate_e8, rate_date, rate_status) for each distinct (ccy, date) in ``pairs``."""
        uniq = pairs[[ccy_col, date_col]].drop_duplicates()
        rows = [(c, d, *self.effective(c, d)) for c, d in zip(uniq[ccy_col], uniq[date_col])]
        out = pd.DataFrame(rows, columns=[ccy_col, date_col, "rate_e8", "rate_date", "rate_status"])
        out["rate_date"] = pd.to_datetime(out["rate_date"])
        return out


def rate_text(rate_e8) -> str | None:
    if rate_e8 is None or (isinstance(rate_e8, float) and np.isnan(rate_e8)) or pd.isna(rate_e8):
        return None
    return money.format_rate(int(rate_e8))


def convert_to_reporting(amounts, currencies, rates_e8, minor_units: dict[str, int], reporting: str) -> list:
    """Convert local minor amounts to reporting minor units; None where no rate is available."""
    dst = minor_units[reporting]
    out = []
    for a, c, r in zip(amounts, currencies, rates_e8):
        if c == reporting:
            out.append(int(a))
        elif r is None or pd.isna(r):
            out.append(None)
        else:
            out.append(money.convert(int(a), minor_units[c], int(r), dst))
    return out


def assert_single_currency(df: pd.DataFrame, group_cols: list[str], ccy_col: str = "currency") -> None:
    """Guard: never aggregate amounts of different currencies without conversion."""
    if df.empty:
        return
    n = df.groupby(group_cols)[ccy_col].nunique()
    if (n > 1).any():
        raise ValueError(f"mixed currencies within aggregation groups: {list(n[n > 1].index[:5])}")
