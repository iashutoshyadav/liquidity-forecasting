"""History helpers: replay lifecycle events and prove history was never overwritten."""
from __future__ import annotations

import pandas as pd

from liqfc.core.enums import LIFECYCLES


def replay(events: pd.DataFrame, key: str, lifecycle: str, ts_col: str) -> tuple[dict[str, str], list[tuple]]:
    """Replay events in time order; return final status per key and invalid transitions."""
    allowed = LIFECYCLES[lifecycle]
    final, errors = {}, []
    ordered = events.sort_values([key, ts_col, events.columns[0]], kind="mergesort")
    for k, g in ordered.groupby(key, sort=False):
        cur = None
        for ev_id, frm, to in zip(g.iloc[:, 0], g["from_status"], g["to_status"]):
            frm = None if pd.isna(frm) else frm
            if frm != cur or to not in allowed.get(cur, set()):
                errors.append((ev_id, k, cur, frm, to))
            cur = to
        final[k] = cur
    return final, errors


def status_as_of(events: pd.DataFrame, key: str, ts_col: str, as_of) -> pd.Series:
    e = events[events[ts_col] < pd.Timestamp(as_of)].sort_values([key, ts_col], kind="mergesort")
    return e.groupby(key)["to_status"].last()


def is_append_only(before: pd.DataFrame, after: pd.DataFrame, key: str) -> bool:
    """True if every row in ``before`` is present unchanged in ``after``."""
    if before.empty:
        return True
    merged = before.merge(after, on=list(before.columns), how="left", indicator=True)
    return bool((merged["_merge"] == "both").all()) and after[key].is_unique
