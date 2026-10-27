import pandas as pd

from liqfc.audit.history import is_append_only, replay, status_as_of
from liqfc.validation.lifecycle import lifecycle_checks


def test_baseline_lifecycles_valid(baseline, base_result):
    tables = {**baseline.tables, **base_result.derived}
    failed = [(c.check_id, c.failures[:3]) for c in lifecycle_checks(tables) if c.status == "FAIL"]
    assert failed == []


def test_invalid_transition_detected():
    ev = pd.DataFrame({"event_id": ["E1", "E2"], "receivable_id": ["R1", "R1"], "from_status": [None, "OPEN"],
                       "to_status": ["OPEN", "WRITTEN_OFF"], "recorded_at": pd.to_datetime(["2026-01-01", "2026-02-01"])})
    final, errors = replay(ev, "receivable_id", "receivable", "recorded_at")
    assert errors and errors[0][0] == "E2"


def test_overdue_receivable_paths(baseline):
    ev = baseline["receivable_status_event"]
    after_overdue = ev[ev["from_status"] == "OVERDUE"]["to_status"]
    assert set(after_overdue) <= {"PAID", "PARTIALLY_PAID", "WRITTEN_OFF"}
    assert "WRITTEN_OFF" in set(after_overdue)


def test_status_as_of_replays_history(baseline):
    ev = baseline["receivable_status_event"]
    paid = baseline["receivable"].query("status == 'PAID'").iloc[0]["receivable_id"]
    hist = ev[ev["receivable_id"] == paid].sort_values("recorded_at")
    first_ts = hist["recorded_at"].iloc[0]
    assert status_as_of(ev, "receivable_id", "recorded_at", first_ts + pd.Timedelta(seconds=1))[paid] == "OPEN"


def test_events_are_append_only(baseline):
    ev = baseline["payable_status_event"]
    extra = ev.iloc[[0]].copy()
    extra["event_id"] = "PSE-2026-999999"
    assert is_append_only(ev, pd.concat([ev, extra]), "event_id")
    tampered = ev.copy()
    tampered.loc[0, "to_status"] = "CANCELLED"
    assert not is_append_only(ev, tampered, "event_id")
