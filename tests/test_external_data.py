import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from liqfc.core import money
from liqfc.core.calendar import BusinessCalendar
from liqfc.core.config import PROJECT_ROOT, load_settings
from liqfc.exporters.csv_json import dataset_hash
from liqfc.generators.receivables import delay_model
from liqfc.pipeline import generate_baseline
from liqfc.validation.checks import structural_checks

EXT = PROJECT_ROOT / "data" / "external"


def test_pinned_files_match_recorded_checksums():
    sources = json.loads((EXT / "SOURCES.json").read_text(encoding="utf-8"))
    for d in sources["datasets"]:
        assert hashlib.sha256((PROJECT_ROOT / d["output_file"]).read_bytes()).hexdigest() == d["output_sha256"]
        assert d["licence"] and d["url"].startswith("https://www.kaggle.com/datasets/") and d["version"]


def test_external_fx_covers_every_business_day(settings):
    fx = pd.read_csv(EXT / "fx_rates_kaggle_v1091.csv", dtype=str)
    cal = BusinessCalendar(settings.raw["calendar"]["holidays"])
    first = cal.roll_backward([settings.start - pd.Timedelta(days=1)])[0]
    days = {str(d) for d in cal.business_days(first, settings.end)}
    for c in ["EUR", "GBP", "INR", "JPY"]:
        assert set(fx.loc[fx["base_currency"] == c, "rate_date"]) == days
    assert (fx["quote_currency"] == "USD").all()
    assert all(money.parse_rate(r) > 0 for r in fx["rate"])


def test_hybrid_rates_come_from_pinned_file(baseline, settings):
    assert settings.external["mode"] == "hybrid"
    r = baseline["currency_rate"]
    assert set(r["source"]) == {"EXTERNAL_KAGGLE_FOREX"}
    fx = pd.read_csv(EXT / "fx_rates_kaggle_v1091.csv", dtype=str)
    got = dict(zip(r["rate_date"].dt.strftime("%Y-%m-%d") + r["base_currency"], r["rate"]))
    want = dict(zip(fx["rate_date"] + fx["base_currency"], fx["rate"]))
    assert got == want


def test_calibrated_delay_model(settings):
    weights, gamma = delay_model(settings)
    cal = json.loads((EXT / "ar_delay_calibration.json").read_text(encoding="utf-8"))
    assert abs(sum(weights.values()) - 1) < 1e-9 and weights["C"] == settings.external["calibrated_class_c_weight"]
    assert gamma["A"] == cal["segments"]["A"]["fitted_delay_gamma"]
    assert gamma["C"] == settings.gen["receivable"]["delay_gamma"]["C"]


def test_calibrated_profile_produces_early_payers(baseline):
    rec = baseline["receivable"]
    rec = rec[(rec["risk_class"] == "A") & ~rec["is_intercompany"] & (rec["instalment_count"] == 1)]
    act = baseline["actual_cash_flow"].groupby("source_id")["actual_date"].min()
    paid = rec[rec["receivable_id"].isin(act.index)]
    early = (paid["receivable_id"].map(act) < paid["due_date"]).mean()
    assert early > 0.10


def test_synthetic_mode_still_works(baseline):
    s = load_settings("starter", data_mode="synthetic")
    assert s.external["mode"] == "synthetic" and s.external["resolved_sha256"] == {}
    ds, _ = generate_baseline(s)
    assert set(ds["currency_rate"]["source"]) == {"SYNTHETIC_GBM"}
    assert len(ds["ledger_entry"]) == len(baseline["ledger_entry"])
    assert [c.check_id for c in structural_checks(ds.tables) if c.status == "FAIL"] == []
    assert dataset_hash(ds.tables) != dataset_hash(baseline.tables)


def test_config_hash_tracks_external_files(settings):
    assert settings.external["resolved_sha256"]
    assert settings.config_hash != load_settings("starter", data_mode="synthetic").config_hash


def test_missing_external_file_is_a_clear_error():
    with pytest.raises(FileNotFoundError, match="prepare_external_data"):
        load_settings("starter", overrides={"external_data": {"fx_file": "data/external/does_not_exist.csv"}})


def test_run_log_states_which_fx_source_is_used():
    from liqfc.generators.fx_rates import fx_source_note
    hybrid = fx_source_note(load_settings("starter", data_mode="hybrid"))
    assert "pinned external file" in hybrid and "ignored" in hybrid
    synthetic = fx_source_note(load_settings("starter", data_mode="synthetic"))
    assert "synthetic random walk" in synthetic and "INR -4.0%" in synthetic
