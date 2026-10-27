"""Reproducibility check: python scripts/reproduce.py --scale full

Runs the complete pipeline twice into separate temporary directories with the same seed and
configuration, compares row counts, SHA-256 checksums, raw bytes and validation summaries, and
writes output/<scale>/reports/reproducibility_report.{json,md}.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from liqfc.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["reproduce", *sys.argv[1:]]))
