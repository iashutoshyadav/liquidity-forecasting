"""Business-day calendar (Mon-Fri, configured global holidays)."""
from __future__ import annotations

import numpy as np
import pandas as pd


class BusinessCalendar:
    def __init__(self, holidays):
        self.holidays = np.array(sorted(holidays), dtype="datetime64[D]")

    @staticmethod
    def _d(dates) -> np.ndarray:
        return np.asarray(pd.to_datetime(dates)).astype("datetime64[D]")

    def is_business_day(self, dates) -> np.ndarray:
        return np.is_busday(self._d(dates), holidays=self.holidays)

    def roll_forward(self, dates) -> np.ndarray:
        return np.busday_offset(self._d(dates), 0, roll="forward", holidays=self.holidays)

    def roll_backward(self, dates) -> np.ndarray:
        return np.busday_offset(self._d(dates), 0, roll="backward", holidays=self.holidays)

    def add_business_days(self, dates, n) -> np.ndarray:
        return np.busday_offset(self._d(dates), n, roll="forward", holidays=self.holidays)

    def business_days(self, start, end) -> np.ndarray:
        days = np.arange(np.datetime64(pd.Timestamp(start).date()), np.datetime64(pd.Timestamp(end).date()) + 1)
        return days[np.is_busday(days, holidays=self.holidays)]

    def month_end_business_days(self, start, end) -> list[np.datetime64]:
        out = []
        for period in pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="M"):
            last = np.datetime64(period.end_time.date())
            out.append(np.busday_offset(last, 0, roll="backward", holidays=self.holidays))
        return out

    def weekly_sweep_dates(self, start, end) -> list[pd.Timestamp]:
        """Last business day of each Mon-Sun week, for weeks whose last business day is in [start, end]."""
        lo = pd.Timestamp(start) - pd.Timedelta(days=7)
        hi = pd.Timestamp(end) + pd.Timedelta(days=7)
        days = pd.to_datetime(self.business_days(lo, hi))
        week = days.to_period("W-SUN")
        last = pd.Series(days, index=week).groupby(level=0).max()
        return [d for d in last if pd.Timestamp(start) <= d <= pd.Timestamp(end)]

    def next_weekday_on_or_after(self, dates, weekday: int) -> np.ndarray:
        """First business day that falls on ``weekday`` (0=Mon) on/after each date; rolls past holidays."""
        d = self._d(dates)
        dow = (d.astype("datetime64[D]").view("int64") - 4) % 7
        shift = (weekday - dow) % 7
        target = d + shift.astype("timedelta64[D]")
        return np.busday_offset(target, 0, roll="forward", holidays=self.holidays)


def to_ts(values) -> pd.Series:
    return pd.to_datetime(pd.Series(values).astype("datetime64[D]"))
