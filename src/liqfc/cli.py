"""Command-line interface.

    python -m liqfc.cli all --scale starter --seed 20261022
    python -m liqfc.cli validate --scale starter
    python -m liqfc.cli reproduce --scale starter
    python -m liqfc.cli docs

Commands run their prerequisite stages automatically (generation is deterministic and fast).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from liqfc.core.config import PROJECT_ROOT, load_settings
from liqfc.exporters.csv_json import write_text
from liqfc.exporters.data_dictionary import render as render_dictionary

STAGE_OF = {"generate": "generate", "forecast": "forecast", "validate": "validate", "export": "all", "all": "all"}


def _out_dir(args) -> Path:
    return Path(args.output) if args.output else PROJECT_ROOT / "output" / args.scale


def main(argv: list[str] | None = None) -> int:
    from liqfc.pipeline import run_pipeline
    from liqfc.validation.reproducibility import reproduce

    p = argparse.ArgumentParser(prog="liqfc", description="Liquidity forecasting and risk monitoring pipeline")
    p.add_argument("command", choices=["generate", "forecast", "validate", "export", "all", "reproduce", "docs"])
    p.add_argument("--scale", choices=["starter", "full"], default="starter")
    p.add_argument("--seed", type=int, default=None, help="override master seed (default from config: 20261022)")
    p.add_argument("--output", default=None, help="output directory (default output/<scale>)")
    p.add_argument("--config-dir", default=None)
    p.add_argument("--with-reproduce", action="store_true", help="for 'all': also run the reproducibility test")
    p.add_argument("--data-mode", choices=["hybrid", "synthetic"], default=None,
                   help="hybrid (default: synthetic core + pinned real FX rates and calibrated delays) or synthetic")
    args = p.parse_args(argv)

    if args.command == "docs":
        path = PROJECT_ROOT / "docs" / "data_dictionary.md"
        write_text(path, render_dictionary())
        print(f"wrote {path}")
        return 0

    settings = load_settings(args.scale, seed=args.seed, config_dir=args.config_dir, data_mode=args.data_mode)
    out = _out_dir(args)
    t0 = time.time()
    if args.command == "reproduce":
        cmd = f"python -m liqfc.cli all --scale {args.scale} --seed {settings.seed} --data-mode {settings.external['mode']}"
        report = reproduce(lambda s, d: run_pipeline(s, d, "all", cmd, log=lambda m: None), settings, out)
        print(json.dumps({k: report[k] for k in ["scale", "seed", "files_compared", "files_matching",
                                                 "validation_summaries_identical", "all_match"]}, indent=2))
        print(f"report: {out / 'reports' / 'reproducibility_report.json'}  ({time.time() - t0:.1f}s)")
        return 0 if report["all_match"] else 1

    stage = STAGE_OF[args.command]
    cmd = f"python -m liqfc.cli {args.command} --scale {args.scale} --seed {settings.seed} --data-mode {settings.external['mode']}"
    info = run_pipeline(settings, out, stage, cmd)
    print(json.dumps({k: v for k, v in info.items() if k != "rows"}, indent=2, default=str))
    if args.with_reproduce and args.command == "all":
        report = reproduce(lambda s, d: run_pipeline(s, d, "all", cmd, log=lambda m: None), settings, out)
        print(f"reproducibility all_match={report['all_match']}")
        from liqfc.exporters.manifest import write_manifest
        write_manifest(out, settings, cmd + " --with-reproduce")
    print(f"elapsed {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
