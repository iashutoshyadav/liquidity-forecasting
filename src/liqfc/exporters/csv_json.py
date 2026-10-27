"""Canonical CSV/JSON serialisation.

Stable column order (schema), stable row order (primary key), UTF-8, LF line endings, fixed
formatting per dtype -- so identical data always produces byte-identical files.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pandas as pd

from liqfc.core.schema import SCHEMA, Table


def _isna(v) -> bool:
    return v is None or (not isinstance(v, str) and pd.isna(v))


def _fmt(dtype: str):
    if dtype == "int":
        return lambda v: "" if _isna(v) else str(int(v))
    if dtype == "date":
        return lambda v: "" if _isna(v) else pd.Timestamp(v).strftime("%Y-%m-%d")
    if dtype == "ts":
        return lambda v: "" if _isna(v) else pd.Timestamp(v).strftime("%Y-%m-%dT%H:%M:%SZ")
    if dtype == "bool":
        return lambda v: "" if _isna(v) else ("true" if bool(v) else "false")
    return lambda v: "" if _isna(v) else str(v)


def _json_val(dtype: str):
    if dtype == "int":
        return lambda v: None if _isna(v) else int(v)
    if dtype == "bool":
        return lambda v: None if _isna(v) else bool(v)
    f = _fmt(dtype)
    return lambda v: None if _isna(v) else f(v)


def canonical_frame(df: pd.DataFrame, table: str | Table) -> pd.DataFrame:
    spec = SCHEMA[table] if isinstance(table, str) else table
    return df[spec.columns].sort_values(list(spec.pk), kind="mergesort").reset_index(drop=True)


def to_csv_text(df: pd.DataFrame, table: str | Table) -> str:
    spec = SCHEMA[table] if isinstance(table, str) else table
    df = canonical_frame(df, spec)
    cols = [[_fmt(f.dtype)(v) for v in df[f.name].tolist()] for f in spec.fields]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(spec.columns)
    w.writerows(zip(*cols)) if len(df) else None
    return buf.getvalue()


def to_json_text(df: pd.DataFrame, table: str | Table) -> str:
    spec = SCHEMA[table] if isinstance(table, str) else table
    df = canonical_frame(df, spec)
    cols = [[_json_val(f.dtype)(v) for v in df[f.name].tolist()] for f in spec.fields]
    names = spec.columns
    lines = [json.dumps(dict(zip(names, row)), ensure_ascii=False, separators=(",", ":")) for row in zip(*cols)]
    return "[\n" + ",\n".join(lines) + ("\n" if lines else "") + "]\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_tables(tables: dict[str, pd.DataFrame], out_dir: Path, formats=("csv", "json"),
                 json_max_rows: int | None = None) -> list[Path]:
    """Write each table in each format; JSON is skipped for tables above ``json_max_rows`` (CSV always written)."""
    written = []
    for name in sorted(tables):
        for fmt in formats:
            if fmt == "json" and json_max_rows is not None and len(tables[name]) > json_max_rows:
                continue
            text = to_csv_text(tables[name], name) if fmt == "csv" else to_json_text(tables[name], name)
            p = Path(out_dir) / fmt / f"{name}.{fmt}"
            write_text(p, text)
            written.append(p)
    return written


def write_plain_csv(df: pd.DataFrame, path: Path) -> None:
    """Canonical CSV for non-schema tables (ground truth, metrics)."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\n")
    write_text(path, buf.getvalue())


def dataset_hash(tables: dict[str, pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for name in sorted(tables):
        h.update(name.encode())
        h.update(to_csv_text(tables[name], name).encode("utf-8"))
    return h.hexdigest()
