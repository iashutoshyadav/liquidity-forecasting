"""Build the pinned external-data files used by the hybrid data mode.

Inputs are the raw Kaggle downloads (not committed). Outputs in data/external/ are committed so
pipeline runs need no internet access and stay reproducible:

  fx_rates_kaggle_v1091.csv     USD-per-unit rates for EUR/GBP/INR/JPY, business days only
  ar_delay_calibration.json     payment-delay statistics and fitted delay parameters
  SOURCES.json                  provenance: URL, version, licence, retrieval date, checksums

Usage:
  python -m kagglehub ... (or kagglehub.dataset_download) to fetch the raw files, then
  python scripts/prepare_external_data.py --fx-csv <daily_forex_rates.csv> --ar-csv <Dataset.csv>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from liqfc.core.calendar import BusinessCalendar  # noqa: E402
from liqfc.core.config import load_settings  # noqa: E402

OUT = ROOT / "data" / "external"
FX_FILE = "fx_rates_kaggle_v1091.csv"
AR_FILE = "ar_delay_calibration.json"
RETRIEVED = "2026-10-27"
Q8 = Decimal("0.00000001")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_fx(raw: Path, settings) -> dict:
    """EUR-based Kaggle rates -> USD per 1 unit (exact Decimal division, 8 dp half-even)."""
    df = pd.read_csv(raw, dtype=str, keep_default_na=False)
    if set(df["base_currency"]) != {"EUR"}:
        raise ValueError("expected EUR-based rates")
    cal = BusinessCalendar(settings.raw["calendar"]["holidays"])
    first = cal.roll_backward([settings.start - pd.Timedelta(days=1)])[0]
    bdays = [f"{d}" for d in cal.business_days(first, settings.end)]
    df = df[df["date"].isin(bdays) & df["currency"].isin(["USD", "GBP", "INR", "JPY"])]
    piv = df.pivot(index="date", columns="currency", values="exchange_rate")
    missing = sorted(set(bdays) - set(piv.index))
    if missing or piv.isna().any().any():
        raise ValueError(f"raw FX file does not cover all business days: {missing[:5]}")
    rows = []
    for d in bdays:
        usd_per_eur = Decimal(piv.at[d, "USD"])
        rates = {"EUR": usd_per_eur}
        for c in ["GBP", "INR", "JPY"]:
            rates[c] = usd_per_eur / Decimal(piv.at[d, c])
        for c in sorted(rates):
            rows.append((d, c, "USD", str(rates[c].quantize(Q8, rounding=ROUND_HALF_EVEN))))
    out = pd.DataFrame(rows, columns=["rate_date", "base_currency", "quote_currency", "rate"])
    OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT / FX_FILE, index=False, lineterminator="\n")
    return {"rows": len(out), "business_days": len(bdays), "currencies": sorted(out["base_currency"].unique())}


def prepare_ar(raw: Path) -> dict:
    """Segment customers by median delay and fit shifted-gamma delay parameters per segment."""
    df = pd.read_csv(raw)
    d = df["Days_Overdue_Delay"]
    stats = df.groupby("Customer_Name")["Days_Overdue_Delay"].agg(["size", "median"])
    stats = stats[stats["size"] >= 20]
    seg = pd.cut(stats["median"], [-np.inf, 2, 10, np.inf], labels=["A", "B", "C"])
    inv = df[df["Customer_Name"].isin(stats.index)].assign(segment=df["Customer_Name"].map(seg))
    segments = {}
    for s, g in inv.groupby("segment", observed=True):
        x = g["Days_Overdue_Delay"]
        lo, hi = x.quantile(0.01), x.quantile(0.99)
        x = x[(x >= lo) & (x <= hi)]                      # trim extreme 1% tails before fitting
        shift = int(np.floor(x.quantile(0.05)))
        y = (x - shift).clip(lower=0)
        mean, var = float(y.mean()), float(y.var())
        segments[str(s)] = {
            "customers": int((seg == s).sum()), "invoices": int(len(g)),
            "invoice_share": round(len(g) / len(inv), 6),
            "observed": {"mean": round(float(g["Days_Overdue_Delay"].mean()), 4),
                         "p10": float(g["Days_Overdue_Delay"].quantile(0.1)),
                         "p50": float(g["Days_Overdue_Delay"].quantile(0.5)),
                         "p90": float(g["Days_Overdue_Delay"].quantile(0.9)),
                         "early_share": round(float((g["Days_Overdue_Delay"] < 0).mean()), 6)},
            "fitted_delay_gamma": {"shape": round(mean**2 / var, 4), "scale": round(var / mean, 4), "shift": shift},
        }
    result = {
        "method": "customers with >=20 invoices segmented by median delay (A <=2d, B 3-10d, C >10d); "
                  "per segment: trim 1%/99% tails, shift = floor(p05), method-of-moments gamma on (delay - shift)",
        "fields_used": ["Customer_Name", "Days_Overdue_Delay"],
        "fields_discarded": "all other 27 columns (amounts, regions, cities, zip codes, dates, payment methods, flags)",
        "overall": {"invoices": int(len(df)), "early_share": round(float((d < 0).mean()), 6),
                    "on_due_share": round(float((d == 0).mean()), 6), "late_share": round(float((d > 0).mean()), 6),
                    "p50": float(d.quantile(0.5)), "p90": float(d.quantile(0.9)), "p99": float(d.quantile(0.99))},
        "segments": segments,
        "segments_absent": [s for s in ["A", "B", "C"] if s not in segments],
    }
    (OUT / AR_FILE).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fx-csv", required=True, type=Path)
    p.add_argument("--ar-csv", required=True, type=Path)
    a = p.parse_args()
    settings = load_settings("starter")
    fx = prepare_fx(a.fx_csv, settings)
    ar = prepare_ar(a.ar_csv)
    sources = {
        "retrieved": RETRIEVED,
        "datasets": [
            {"role": "currency_rate values (fx_source: kaggle)",
             "url": "https://www.kaggle.com/datasets/asaniczka/forex-exchange-rate-since-2004-updated-daily",
             "dataset": "asaniczka/forex-exchange-rate-since-2004-updated-daily", "version": 1091,
             "licence": "ODC Attribution License (ODC-By)", "attribution": "Forex Exchange Rates Since 2004 by asaniczka (Kaggle)",
             "raw_file": "daily_forex_rates.csv", "raw_sha256": sha256(a.fx_csv),
             "transformations": "kept USD, GBP, INR, JPY vs EUR base for business days 2025-12-31..2026-06-30 "
                                "(project calendar); converted to USD per 1 unit: EUR = USD/EUR; X = (USD/EUR)/(X/EUR); "
                                "exact Decimal division, 8 dp ROUND_HALF_EVEN",
             "fields_discarded": "currency_name; all other 172 currencies; weekend/holiday rows; dates outside the window",
             "output_file": f"data/external/{FX_FILE}", "output_sha256": sha256(OUT / FX_FILE), **fx},
            {"role": "receivable payment-delay calibration (receivable_delay_profile: calibrated)",
             "url": "https://www.kaggle.com/datasets/sonalisingh1411/accounts-receivable-and-payment-delay-analysis",
             "dataset": "sonalisingh1411/accounts-receivable-and-payment-delay-analysis", "version": 1,
             "licence": "Apache License 2.0", "attribution": "Accounts Receivable & Payment Delay Analysis by Sonali Singh (Kaggle)",
             "raw_file": "Dataset.csv", "raw_sha256": sha256(a.ar_csv),
             "transformations": ar["method"], "fields_used": ar["fields_used"], "fields_discarded": ar["fields_discarded"],
             "output_file": f"data/external/{AR_FILE}", "output_sha256": sha256(OUT / AR_FILE),
             "note": "only aggregate statistics are stored; no rows are redistributed"},
        ],
    }
    (OUT / "SOURCES.json").write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"fx": fx, "ar_segments": {k: v["fitted_delay_gamma"] | {"share": v["invoice_share"]}
                                                for k, v in ar["segments"].items()},
                      "absent": ar["segments_absent"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
