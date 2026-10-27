"""Source manifest: every output file with row count, size and SHA-256, plus run provenance."""
from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

import numpy as np
import pandas as pd

from liqfc.exporters.csv_json import write_text

EXCLUDED = {"manifest.json", "reproducibility_report.json", "reproducibility_report.md"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(path: Path):
    if path.suffix == ".csv":
        with open(path, "rb") as fh:
            return max(sum(1 for _ in fh) - 1, 0)
    if path.suffix == ".json" and path.parent.name == "json":
        with open(path, "rb") as fh:
            return max(sum(1 for _ in fh) - 2, 0)
    return None


def file_entries(root: Path) -> list[dict]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in EXCLUDED:
            out.append({"path": p.relative_to(root).as_posix(), "rows": _rows(p), "bytes": p.stat().st_size,
                        "sha256": sha256_file(p)})
    return out


def _provenance(settings) -> dict:
    ext = settings.external
    out = {"mode": ext["mode"], "personal_data": "none", "statement": "docs/provenance.md",
           "synthetic": "entities, accounts, ledger, receivables, payables, forecasts, statements, scenarios, anomalies"}
    if ext["resolved_sha256"]:
        sources = json.loads(settings.external_path("sources_file").read_text(encoding="utf-8"))
        used = set(ext["resolved_sha256"])
        out["external_datasets"] = [
            {k: d[k] for k in ["role", "url", "dataset", "version", "licence", "attribution", "output_file", "output_sha256"]}
            for d in sources["datasets"] if d["output_file"] in used]
        out["retrieved"] = sources["retrieved"]
        out["external_files_sha256_at_run"] = ext["resolved_sha256"]
    return out


def write_manifest(root: Path, settings, command: str) -> dict:
    manifest = {
        "project": settings.raw["project"]["name"],
        "generator_version": settings.generator_version,
        "calculation_version": settings.calculation_version,
        "seed": settings.seed,
        "scale": settings.scale_name,
        "scale_parameters": settings.scale,
        "config_hash": settings.config_hash,
        "generation_timestamp": settings.raw["generation_timestamp"],
        "command": command,
        "environment": {"python": platform.python_version(), "pandas": pd.__version__, "numpy": np.__version__},
        "data_provenance": _provenance(settings),
        "files": file_entries(root),
    }
    write_text(root / "manifest.json", json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    return manifest
