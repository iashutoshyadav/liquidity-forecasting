"""Reproducibility test: generate twice into separate temp directories and compare outputs."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from liqfc.exporters.csv_json import write_text


def compare_runs(run_a: Path, run_b: Path) -> dict:
    ma = json.loads((run_a / "manifest.json").read_text(encoding="utf-8"))
    mb = json.loads((run_b / "manifest.json").read_text(encoding="utf-8"))
    fa = {f["path"]: f for f in ma["files"]}
    fb = {f["path"]: f for f in mb["files"]}
    files = []
    for p in sorted(set(fa) | set(fb)):
        a, b = fa.get(p), fb.get(p)
        files.append({"path": p, "in_run_a": a is not None, "in_run_b": b is not None,
                      "rows_a": a and a["rows"], "rows_b": b and b["rows"],
                      "sha256_a": a and a["sha256"], "sha256_b": b and b["sha256"],
                      "bytes_identical": bool(a and b and (run_a / p).read_bytes() == (run_b / p).read_bytes()),
                      "match": bool(a and b and a["sha256"] == b["sha256"] and a["rows"] == b["rows"])})
    va = (run_a / "reports" / "validation_summary.json").read_text(encoding="utf-8")
    vb = (run_b / "reports" / "validation_summary.json").read_text(encoding="utf-8")
    mismatched = [f["path"] for f in files if not f["match"]]
    return {
        "seed": ma["seed"], "scale": ma["scale"], "config_hash_a": ma["config_hash"], "config_hash_b": mb["config_hash"],
        "files_compared": len(files), "files_matching": len(files) - len(mismatched), "mismatched_files": mismatched,
        "validation_summaries_identical": va == vb,
        "all_match": not mismatched and va == vb and ma["config_hash"] == mb["config_hash"],
        "method": "two independent end-to-end runs into separate temporary directories; compared row counts, "
                  "SHA-256 checksums, raw bytes and validation summaries",
        "files": files,
    }


def reproduce(run_fn, settings, out_dir: Path, keep_temp: bool = False) -> dict:
    """``run_fn(settings, directory)`` performs one complete run."""
    tmp = Path(tempfile.mkdtemp(prefix="liqfc-repro-"))
    try:
        a, b = tmp / "run_a", tmp / "run_b"
        run_fn(settings, a)
        run_fn(settings, b)
        report = compare_runs(a, b)
    finally:
        if not keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)
    rep_dir = Path(out_dir) / "reports"
    write_text(rep_dir / "reproducibility_report.json", json.dumps(report, indent=2) + "\n")
    lines = ["# Reproducibility report", "", f"- Scale: `{report['scale']}`  Seed: `{report['seed']}`",
             f"- Files compared: {report['files_compared']}, matching: {report['files_matching']}",
             f"- Validation summaries identical: {report['validation_summaries_identical']}",
             f"- **All outputs match: {report['all_match']}**", "", "| File | Rows | SHA-256 (run A) | Match |", "|---|---|---|---|"]
    lines += [f"| `{f['path']}` | {f['rows_a']} | `{(f['sha256_a'] or '')[:16]}…` | {'yes' if f['match'] else 'NO'} |"
              for f in report["files"]]
    write_text(rep_dir / "reproducibility_report.md", "\n".join(lines) + "\n")
    return report
