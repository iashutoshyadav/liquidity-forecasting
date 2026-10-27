"""Dataset container: named DataFrames keyed by schema table name."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from liqfc.core.schema import INPUT_TABLES, SCHEMA, Table

DATETIME_UNIT = "datetime64[ns]"


def normalize_datetimes(df: pd.DataFrame, spec: Table) -> pd.DataFrame:
    """Give every date/ts column one resolution (pandas 3 infers s/us/ns per column)."""
    for f in spec.fields:
        if f.dtype in ("date", "ts") and f.name in df.columns and df[f.name].dtype != DATETIME_UNIT:
            df[f.name] = pd.to_datetime(df[f.name]).astype(DATETIME_UNIT)
    return df


@dataclass
class Dataset:
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    label: str = ""

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.tables[name]

    def __setitem__(self, name: str, df: pd.DataFrame) -> None:
        if name not in SCHEMA:
            raise KeyError(f"{name} is not a schema table")
        self.tables[name] = df

    def __contains__(self, name: str) -> bool:
        return name in self.tables

    def copy(self, label: str | None = None) -> "Dataset":
        return Dataset({k: v.copy(deep=True) for k, v in self.tables.items()}, label if label is not None else self.label)

    def input_only(self) -> "Dataset":
        return Dataset({k: self.tables[k] for k in INPUT_TABLES if k in self.tables}, self.label)

    def row_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in sorted(self.tables.items())}

    def conform(self) -> "Dataset":
        """Restrict every table to its schema columns in schema order, sorted by primary key."""
        for name, df in list(self.tables.items()):
            spec = SCHEMA[name]
            missing = [c for c in spec.columns if c not in df.columns]
            if missing:
                raise ValueError(f"{name} missing columns {missing}")
            out = df[spec.columns].sort_values(list(spec.pk), kind="mergesort").reset_index(drop=True)
            self.tables[name] = normalize_datetimes(out, spec)
        return self
