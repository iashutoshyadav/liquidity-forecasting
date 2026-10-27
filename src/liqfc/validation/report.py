"""Run all validations for a dataset stage and summarise them (machine-readable)."""
from __future__ import annotations

import pandas as pd

from liqfc.validation.checks import FAILURE_COLUMNS, CheckResult, structural_checks
from liqfc.validation.foreign_keys import fk_checks, sqlite_fk_check
from liqfc.validation.lifecycle import lifecycle_checks
from liqfc.validation.reconciliation import derived_checks, input_business_checks, volume_check


def run_validation(stage: str, inputs: dict[str, pd.DataFrame], settings, cal, derived: dict[str, pd.DataFrame] | None = None,
                   sql: tuple[str, str, dict] | None = None, check_volume: bool = False) -> tuple[dict, pd.DataFrame]:
    tables = dict(inputs)
    if derived:
        tables.update(derived)
    results: list[CheckResult] = []
    results += structural_checks(tables)
    results += fk_checks(tables)
    results += lifecycle_checks(tables)
    results += input_business_checks(inputs, settings, cal)
    if check_volume:
        results.append(volume_check(inputs, settings))
    if derived:
        results += derived_checks(inputs, derived, settings, cal)
    if sql:
        results.append(sqlite_fk_check(*sql))
    return summarise(stage, results), failures_frame(results)


def summarise(stage: str, results: list[CheckResult]) -> dict:
    checks = [r.summary() for r in results]
    passed = sum(1 for c in checks if c["status"] == "PASS")
    by_cat: dict[str, dict] = {}
    for c in checks:
        b = by_cat.setdefault(c["category"], {"passed": 0, "failed": 0})
        b["passed" if c["status"] == "PASS" else "failed"] += 1
    return {"stage": stage, "total_checks": len(checks), "passed": passed, "failed": len(checks) - passed,
            "pass_rate": round(passed / len(checks), 6) if checks else None, "by_category": by_cat,
            "failed_checks": [c for c in checks if c["status"] == "FAIL"], "checks": checks}


def failures_frame(results: list[CheckResult]) -> pd.DataFrame:
    rows = [row for r in results if r.emit_exceptions for row in r.failure_rows()]
    return pd.DataFrame(rows, columns=FAILURE_COLUMNS)
