"""Validation primitives and structural checks (PK, required, enum, format, unique, date order)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from liqfc.core.schema import DATE_ORDER_RULES, SCHEMA

FAILURE_COLUMNS = ["rule_id", "issue_type", "affected_table", "affected_record_id", "severity",
                   "observed_value", "expected_value", "decision", "details"]


@dataclass
class CheckResult:
    check_id: str
    category: str
    name: str
    table: str
    records_checked: int
    failures: list = field(default_factory=list)
    issue_type: str = "BUSINESS_RULE_VIOLATION"
    severity: str = "HIGH"
    decision: str = "HELD_FOR_REVIEW"
    emit_exceptions: bool = True

    @property
    def status(self) -> str:
        return "PASS" if not self.failures else "FAIL"

    def summary(self) -> dict:
        return {"check_id": self.check_id, "category": self.category, "name": self.name, "table": self.table,
                "status": self.status, "records_checked": int(self.records_checked), "failures": len(self.failures),
                "sample": [str(f[0]) for f in self.failures[:5]]}

    def failure_rows(self) -> list[dict]:
        return [{"rule_id": self.check_id, "issue_type": self.issue_type, "affected_table": self.table,
                 "affected_record_id": str(rid), "severity": self.severity, "observed_value": str(obs),
                 "expected_value": str(exp), "decision": self.decision, "details": det}
                for rid, obs, exp, det in self.failures]


def record_ids(df: pd.DataFrame, table: str) -> pd.Series:
    pk = list(SCHEMA[table].pk)
    if len(pk) == 1:
        return df[pk[0]].astype(str)
    return df[pk].astype(str).agg("|".join, axis=1)


def structural_checks(tables: dict[str, pd.DataFrame]) -> list[CheckResult]:
    out: list[CheckResult] = []
    for name in sorted(tables):
        df, spec = tables[name], SCHEMA[name]
        rid = record_ids(df, name)
        dup = df.duplicated(list(spec.pk), keep=False)
        out.append(CheckResult(f"STR-PK-{name}", "STRUCTURAL", "Primary-key uniqueness", name, len(df),
                               [(r, "duplicate", "unique", "duplicate primary key") for r in rid[dup]],
                               "PK_VIOLATION", "CRITICAL", "REJECTED"))
        req, enum, fmt, unq = [], [], [], []
        for f in spec.fields:
            col = df[f.name]
            null = col.isna() | col.map(lambda v: isinstance(v, str) and v == "").astype(bool)
            if f.required:
                req += [(r, "null", "value", f"{f.name} required") for r in rid[null]]
            present = ~null
            if f.enum:
                bad = present & ~col.isin(f.enum)
                enum += [(r, v, "|".join(f.enum), f"{f.name} not allowed") for r, v in zip(rid[bad], col[bad])]
            if f.pattern:
                rx = re.compile(f.pattern)
                bad = present & ~col.map(lambda v: isinstance(v, str) and bool(rx.match(v))).astype(bool)
                fmt += [(r, v, f.pattern, f"{f.name} format") for r, v in zip(rid[bad], col[bad])]
            if f.unique:
                bad = present & col.duplicated(keep=False)
                unq += [(r, v, "unique", f"{f.name} duplicated") for r, v in zip(rid[bad], col[bad])]
        for combo in spec.unique_together:
            bad = df.duplicated(list(combo), keep=False)
            unq += [(r, "duplicate", "unique", f"{combo} duplicated") for r in rid[bad]]
        out.append(CheckResult(f"STR-REQ-{name}", "STRUCTURAL", "Required fields", name, len(df), req, "REQUIRED_MISSING"))
        out.append(CheckResult(f"STR-ENUM-{name}", "STRUCTURAL", "Allowed enum values", name, len(df), enum, "ENUM_VIOLATION"))
        out.append(CheckResult(f"STR-FMT-{name}", "STRUCTURAL", "Identifier/field format", name, len(df), fmt, "FORMAT_VIOLATION"))
        out.append(CheckResult(f"STR-UNQ-{name}", "STRUCTURAL", "Uniqueness constraints", name, len(df), unq, "PK_VIOLATION"))
    for table, a, b, allow_eq in DATE_ORDER_RULES:
        if table not in tables:
            continue
        df = tables[table]
        both = df[a].notna() & df[b].notna()
        bad = both & ((df[a] > df[b]) if allow_eq else (df[a] >= df[b]))
        rid = record_ids(df, table)
        out.append(CheckResult(f"STR-DATE-{table}-{a}-{b}", "STRUCTURAL", f"Date order {a} {'<=' if allow_eq else '<'} {b}",
                               table, int(both.sum()),
                               [(r, f"{x:%Y-%m-%d}", f"{a} before {b}", "date order") for r, x in zip(rid[bad], df.loc[bad, a])],
                               "DATE_ORDER_VIOLATION"))
    return out
