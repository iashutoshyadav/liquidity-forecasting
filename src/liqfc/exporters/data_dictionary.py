"""Render the data dictionary (Markdown) from the schema specs."""
from __future__ import annotations

from liqfc.core.schema import GROUND_TRUTH_COLUMNS, TABLES


def render() -> str:
    lines = ["# Data dictionary", "",
             "Generated from `src/liqfc/core/schema.py` (`python -m liqfc.cli docs`). Do not edit by hand.", "",
             "Types: `str`, `int` (signed integer; money columns are **minor units** of the stated currency), "
             "`date` (YYYY-MM-DD), `ts` (UTC, YYYY-MM-DDTHH:MM:SSZ), `decimal` (exact decimal string), `bool`.", ""]
    for layer, title in [("input", "Input tables (generated source data; what detectors see)"),
                         ("derived", "Derived tables (calculated by the engine)")]:
        lines += [f"## {title}", ""]
        for t in [t for t in TABLES if t.layer == layer]:
            lines += [f"### `{t.name}` — {t.entity}", "", t.desc, "",
                      f"Primary key: `{', '.join(t.pk)}`" + (f"; unique: {', '.join('(' + ', '.join(c) + ')' for c in t.unique_together)}"
                                                             if t.unique_together else ""), "",
                      "| Field | Type | Req | Allowed values / pattern | Unit | Unique | FK | Validation / derivation | Description |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for f in t.fields:
                allowed = ", ".join(f.enum) if f.enum else (f"`{f.pattern}`" if f.pattern else "")
                lines.append(f"| `{f.name}` | {f.dtype} | {'Y' if f.required else 'N'} | {allowed} | {f.unit} | "
                             f"{'Y' if f.unique or (len(t.pk) == 1 and f.name == t.pk[0]) else ''} | {f.fk or ''} | "
                             f"{f.rule} | {f.desc} |".replace("|  |", "| |"))
            lines.append("")
    lines += ["## Ground truth (separate file, never joined into detector inputs)", "",
              "`ground_truth/ground_truth.csv|json` columns: " + ", ".join(f"`{c}`" for c in GROUND_TRUTH_COLUMNS), ""]
    return "\n".join(lines)
