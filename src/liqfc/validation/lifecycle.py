"""Lifecycle validation: every event sequence follows the allowed transition map and the
entity's status column equals its last event."""
from __future__ import annotations

import pandas as pd

from liqfc.audit.history import replay
from liqfc.validation.checks import CheckResult

LIFECYCLE_SOURCES = [
    ("receivable", "receivable_status_event", "receivable_id", "recorded_at", "receivable", "status"),
    ("payable", "payable_status_event", "payable_id", "recorded_at", "payable", "status"),
    ("bank_account", "account_status_event", "account_id", "recorded_at", "bank_account", "status"),
    ("forecast_run", "forecast_run_event", "forecast_run_id", "event_ts", "forecast_run", "run_status"),
]


def lifecycle_checks(tables: dict[str, pd.DataFrame]) -> list[CheckResult]:
    out = []
    for lc, ev_t, key, ts, ent_t, scol in LIFECYCLE_SOURCES:
        if ev_t not in tables or ent_t not in tables:
            continue
        final, errors = replay(tables[ev_t], key, lc, ts)
        fails = [(ev, f"{cur}->{to} (declared from {frm})", "allowed transition", f"{lc} {k}")
                 for ev, k, cur, frm, to in errors]
        ent = tables[ent_t]
        mism = ent[ent[key].map(final) != ent[scol]]
        fails += [(k, s, final.get(k), "status differs from last event") for k, s in zip(mism[key], mism[scol])]
        out.append(CheckResult(f"STR-LIFE-{lc}", "STRUCTURAL", f"{lc} lifecycle transitions", ev_t, len(tables[ev_t]),
                               fails, "LIFECYCLE_VIOLATION"))
    return out
