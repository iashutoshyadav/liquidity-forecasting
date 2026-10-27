"""Foreign-key integrity: declared FKs, polymorphic source FKs, and an independent SQLite check."""
from __future__ import annotations

import sqlite3

import pandas as pd

from liqfc.core.schema import POLYMORPHIC_FKS, SCHEMA, SOURCE_TARGETS
from liqfc.validation.checks import CheckResult, record_ids


def fk_checks(tables: dict[str, pd.DataFrame]) -> list[CheckResult]:
    out = []
    for name in sorted(tables):
        df, spec = tables[name], SCHEMA[name]
        rid = record_ids(df, name)
        for f in spec.fields:
            if not f.fk:
                continue
            ptable, pcol = f.fk.split(".")
            if ptable not in tables:
                continue
            vals = df[f.name]
            present = vals.notna()
            bad = present & ~vals.isin(set(tables[ptable][pcol]))
            out.append(CheckResult(f"STR-FK-{name}-{f.name}", "STRUCTURAL", f"FK {f.name} -> {f.fk}", name,
                                   int(present.sum()), [(r, v, f.fk, "orphan reference") for r, v in zip(rid[bad], vals[bad])],
                                   "FK_VIOLATION", "CRITICAL", "REJECTED"))
    for table, tcol, icol in POLYMORPHIC_FKS:
        if table not in tables:
            continue
        df = tables[table]
        rid = record_ids(df, table)
        fails, checked = [], 0
        for stype, (ptable, pcol) in SOURCE_TARGETS.items():
            m = df[tcol] == stype
            checked += int(m.sum())
            bad = m & ~df[icol].isin(set(tables[ptable][pcol]))
            fails += [(r, v, f"{ptable}.{pcol}", "orphan polymorphic reference") for r, v in zip(rid[bad], df.loc[bad, icol])]
        out.append(CheckResult(f"STR-FK-{table}-{icol}", "STRUCTURAL", f"Polymorphic FK {tcol}/{icol}", table, checked,
                               fails, "FK_VIOLATION", "CRITICAL", "REJECTED"))
    return out


def sqlite_fk_check(schema_sql: str, seed_sql: str, expected_counts: dict[str, int]) -> CheckResult:
    """Load the exported SQL into in-memory SQLite and run PRAGMA foreign_key_check + row counts."""
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(schema_sql)
        con.executescript(seed_sql)
        fails = [(f"{t}#{rowid}", parent, "existing parent", "sqlite foreign_key_check")
                 for t, rowid, parent, _ in con.execute("PRAGMA foreign_key_check").fetchall()]
        for t, n in expected_counts.items():
            got = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            if got != n:
                fails.append((t, got, n, "row count mismatch after SQL load"))
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            fails.append(("database", integrity, "ok", "sqlite integrity_check"))
        total = sum(expected_counts.values())
    finally:
        con.close()
    return CheckResult("STR-SQL-FK", "STRUCTURAL", "SQLite seed load + foreign_key_check", "sql_seed", total, fails,
                       "FK_VIOLATION", "CRITICAL", "REJECTED")
