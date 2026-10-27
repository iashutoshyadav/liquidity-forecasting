"""Append-only audit and forecast-run event logs with deterministic timestamps."""
from __future__ import annotations

import json

import pandas as pd

from liqfc.core.enums import LIFECYCLES


class AppendOnlyLog:
    """Rows can only be appended; existing rows are never modified or removed."""

    def __init__(self, columns: list[str]):
        self.columns = columns
        self._rows: list[tuple] = []

    def append(self, **values) -> None:
        self._rows.append(tuple(values[c] for c in self.columns))

    def __len__(self) -> int:
        return len(self._rows)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(list(self._rows), columns=self.columns)


class RunLifecycle:
    """Tracks forecast-run status transitions, rejecting invalid ones."""

    def __init__(self, id_year: int):
        self.id_year = id_year
        self.log = AppendOnlyLog(["forecast_run_id", "from_status", "to_status", "event_ts", "actor_role", "reason"])
        self.status: dict[str, str | None] = {}

    def transition(self, run_id: str, to_status: str, ts: pd.Timestamp, actor: str, reason: str) -> None:
        cur = self.status.get(run_id)
        allowed = LIFECYCLES["forecast_run"].get(cur, set())
        if to_status not in allowed:
            raise ValueError(f"invalid forecast run transition {cur} -> {to_status} for {run_id}")
        self.log.append(forecast_run_id=run_id, from_status=cur, to_status=to_status, event_ts=ts, actor_role=actor, reason=reason)
        self.status[run_id] = to_status

    def frame(self) -> pd.DataFrame:
        df = self.log.frame().sort_values(["event_ts", "forecast_run_id"], kind="mergesort").reset_index(drop=True)
        df.insert(0, "event_id", [f"FRE-{self.id_year}-{i:06d}" for i in range(1, len(df) + 1)])
        return df


class AuditTrail:
    def __init__(self, id_year: int):
        self.id_year = id_year
        self.log = AppendOnlyLog(["event_ts", "actor_role", "action", "object_type", "object_id", "details"])

    def record(self, ts, actor: str, action: str, object_type: str, object_id: str, **details) -> None:
        self.log.append(event_ts=ts, actor_role=actor, action=action, object_type=object_type, object_id=object_id,
                        details=json.dumps(details, sort_keys=True, default=str))

    def frame(self) -> pd.DataFrame:
        df = self.log.frame().sort_values(["event_ts", "action", "object_id"], kind="mergesort").reset_index(drop=True)
        df.insert(0, "audit_id", [f"AUD-{self.id_year}-{i:06d}" for i in range(1, len(df) + 1)])
        return df
