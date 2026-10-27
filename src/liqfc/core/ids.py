"""Deterministic identifier formatting and patterns.

IDs are assigned once, after a stable sort, and never re-used or changed.
"""
from __future__ import annotations

import re

PATTERNS = {
    "entity": r"^ENT-\d{6}$",
    "account": r"^BANKACC-ENT-\d{6}-\d{3}$",
    "ledger": r"^LED-\d{4}-\d{8}$",
    "cf": r"^CF-\d{4}-\d{6}$",
    "run": r"^FCAST-\d{4}-\d{2}-\d{2}-\d{3}$",
    "receivable": r"^RCV-\d{4}-\d{6}$",
    "payable": r"^PAY-\d{4}-\d{6}$",
    "actual": r"^ACT-\d{4}-\d{6}$",
    "statement": r"^STMT-\d{4}-\d{6}$",
    "rate": r"^FX-\d{4}-\d{2}-\d{2}-[A-Z]{3}-[A-Z]{3}$",
    "scenario": r"^SCN-[A-Z_]+$",
    "assumption": r"^ASM-[A-Z_]+-\d{2}$",
    "link": r"^FSL-\d{4}-\d{6}$",
    "receivable_event": r"^RSE-\d{4}-\d{6}$",
    "payable_event": r"^PSE-\d{4}-\d{6}$",
    "account_event": r"^ASE-\d{4}-\d{6}$",
    "run_event": r"^FRE-\d{4}-\d{6}$",
    "exception": r"^EXC-\d{4}-\d{6}$",
    "alert": r"^ALR-\d{4}-\d{6}$",
    "audit": r"^AUD-\d{4}-\d{6}$",
    "customer": r"^CUST-\d{5}$",
    "supplier": r"^SUPP-\d{5}$",
    "run_line": r"^FCAST-\d{4}-\d{2}-\d{2}-\d{3}-L\d{6}$",
    "attribution": r"^FCAST-\d{4}-\d{2}-\d{2}-\d{3}-V\d{6}$",
    "anomaly": r"^ANOM-\d{4}$",
}
_COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}


def matches(kind: str, value: str) -> bool:
    return bool(_COMPILED[kind].match(value))


def entity_id(n: int) -> str:
    return f"ENT-{n:06d}"


def account_id(entity: str, n: int) -> str:
    return f"BANKACC-{entity}-{n:03d}"


def seq_id(prefix: str, year: int, n: int, width: int = 6) -> str:
    return f"{prefix}-{year}-{n:0{width}d}"


def ledger_id(year: int, n: int) -> str:
    return seq_id("LED", year, n, 8)


def run_id(cutoff_date, n: int) -> str:
    return f"FCAST-{cutoff_date:%Y-%m-%d}-{n:03d}"


def rate_id(rate_date, base: str, quote: str) -> str:
    return f"FX-{rate_date:%Y-%m-%d}-{base}-{quote}"


def seq_ids(prefix: str, year: int, count: int, start: int = 1, width: int = 6) -> list[str]:
    return [f"{prefix}-{year}-{i:0{width}d}" for i in range(start, start + count)]
