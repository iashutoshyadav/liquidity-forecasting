"""SQL schema (DDL with PK/FK/NOT NULL/CHECK constraints) and seed INSERTs from the schema specs.

Types: money/counts INTEGER (minor units), dates/timestamps/decimals TEXT in canonical ISO /
exact decimal form (SQLite has no exact decimal type; TEXT avoids binary floating point).
"""
from __future__ import annotations

import pandas as pd

from liqfc.core.schema import SCHEMA
from liqfc.exporters.csv_json import _fmt, canonical_frame

SQL_TYPE = {"str": "TEXT", "int": "INTEGER", "date": "TEXT", "ts": "TEXT", "decimal": "TEXT", "bool": "INTEGER"}


def ddl(table_names: list[str]) -> str:
    parts = ["-- Generated from liqfc.core.schema. Load with: sqlite3 db < schema.sql && sqlite3 db < seed.sql",
             "PRAGMA foreign_keys = OFF;"]
    for name in table_names:
        spec = SCHEMA[name]
        cols = []
        for f in spec.fields:
            c = f'  "{f.name}" {SQL_TYPE[f.dtype]}'
            if f.required:
                c += " NOT NULL"
            if f.enum:
                vals = ", ".join("'" + v + "'" for v in f.enum)
                c += f' CHECK ("{f.name}" IN ({vals}))'
            if f.dtype == "bool":
                c += f' CHECK ("{f.name}" IN (0, 1))'
            if f.dtype == "date":
                c += f" CHECK (\"{f.name}\" GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')"
            if f.unique and len(spec.pk) == 1 and f.name != spec.pk[0]:
                c += " UNIQUE"
            cols.append(c)
        cols.append("  PRIMARY KEY (" + ", ".join(f'"{c}"' for c in spec.pk) + ")")
        for combo in spec.unique_together:
            cols.append("  UNIQUE (" + ", ".join(f'"{c}"' for c in combo) + ")")
        for f in spec.fields:
            if f.fk and f.fk.split(".")[0] in table_names:
                pt, pc = f.fk.split(".")
                cols.append(f'  FOREIGN KEY ("{f.name}") REFERENCES "{pt}" ("{pc}")')
        parts.append(f'CREATE TABLE "{name}" (\n' + ",\n".join(cols) + "\n);")
    return "\n\n".join(parts) + "\n"


def _sql_literal(dtype: str):
    fmt = _fmt(dtype)

    def lit(v):
        s = fmt(v)
        if s == "":
            return "NULL"
        if dtype == "int":
            return s
        if dtype == "bool":
            return "1" if s == "true" else "0"
        return "'" + s.replace("'", "''") + "'"
    return lit


def seed(tables: dict[str, pd.DataFrame], table_names: list[str], batch: int = 500) -> str:
    out = ["PRAGMA foreign_keys = OFF;", "BEGIN TRANSACTION;"]
    for name in table_names:
        spec = SCHEMA[name]
        df = canonical_frame(tables[name], spec)
        cols = [[_sql_literal(f.dtype)(v) for v in df[f.name].tolist()] for f in spec.fields]
        header = f'INSERT INTO "{name}" (' + ", ".join(f'"{c}"' for c in spec.columns) + ") VALUES\n"
        rows = ["(" + ", ".join(r) + ")" for r in zip(*cols)]
        for i in range(0, len(rows), batch):
            out.append(header + ",\n".join(rows[i:i + batch]) + ";")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"
