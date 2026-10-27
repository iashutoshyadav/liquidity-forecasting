"""Ground-truth label store. Kept outside the Dataset so detectors can never see it."""
from __future__ import annotations

import pandas as pd

from liqfc.core.schema import GROUND_TRUTH_COLUMNS

STAGE = "POST_BASELINE_VALIDATION"


class GroundTruth:
    def __init__(self):
        self._rows: list[dict] = []

    def add(self, issue_type: str, table: str, record_id: str, field: str, original, injected, rule: str,
            decision: str, notes: str = "") -> None:
        self._rows.append({
            "issue_type": issue_type, "affected_table": table, "affected_record_id": record_id,
            "affected_field": field, "original_value": "" if original is None else str(original),
            "injected_value": "" if injected is None else str(injected), "expected_detection_rule": rule,
            "expected_decision": decision, "injection_stage": STAGE, "notes": notes,
        })

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self._rows, columns=[c for c in GROUND_TRUTH_COLUMNS if c != "anomaly_id"])
        df = df.sort_values(["issue_type", "affected_record_id"], kind="mergesort").reset_index(drop=True)
        df.insert(0, "anomaly_id", [f"ANOM-{i:04d}" for i in range(1, len(df) + 1)])
        return df[GROUND_TRUTH_COLUMNS]
