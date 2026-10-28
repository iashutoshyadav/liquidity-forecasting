"""The HTML dashboard is a faithful, self-contained, deterministic view of the run's outputs."""
import inspect
import json
import re

import pandas as pd
import pytest

from liqfc.exporters import dashboard
from liqfc.exporters.dashboard import build_dashboard, render
from liqfc.pipeline import run_pipeline


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory, settings):
    out = tmp_path_factory.mktemp("dash") / "starter"
    run_pipeline(settings, out, "all", log=lambda m: None)
    return out


def _payload(html):
    m = re.search(r'<script type="application/json" id="dash-data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def _derived(out, name):
    return pd.read_csv(out / "dataset" / "derived" / "csv" / f"{name}.csv")


def test_dashboard_written_and_in_manifest(run_dir):
    assert (run_dir / "reports" / "dashboard.html").is_file()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "reports/dashboard.html" in {f["path"] for f in manifest["files"]}


def test_dashboard_is_self_contained(run_dir):
    html = (run_dir / "reports" / "dashboard.html").read_text(encoding="utf-8")
    assert "<script src" not in html and "<link" not in html
    assert set(re.findall(r"https?://[^\s\"'<>]+", html)) <= {"http://www.w3.org/2000/svg"}


def test_dashboard_numbers_match_outputs(run_dir):
    d = _payload((run_dir / "reports" / "dashboard.html").read_text(encoding="utf-8"))
    cons = _derived(run_dir, "consolidated_cash_position")
    grp = cons[cons["level"] == "GROUP"].sort_values("position_date")
    assert d["group"]["cash"] == grp["closing_balance_reporting_minor"].tolist()
    runs = _derived(run_dir, "forecast_run")
    assert [r["id"] for r in d["runs"]] == sorted(runs["forecast_run_id"])
    assert sum(d["reconciliation"]["items"].values()) == len(_derived(run_dir, "bank_reconciliation_item"))
    pos = _derived(run_dir, "daily_cash_position")
    assert len(d["reconciliation"]["breaks"]) == (pos["reconciliation_status"] == "BREAK").sum()
    assert len(d["alerts"]) == len(_derived(run_dir, "liquidity_alert"))
    ev = json.loads((run_dir / "reports" / "evaluation_summary.json").read_text(encoding="utf-8"))
    assert d["detection"]["by_type"] == ev["detection"]["injected"]


def test_variance_causes_add_up(run_dir):
    d = _payload((run_dir / "reports" / "dashboard.html").read_text(encoding="utf-8"))
    for f in d["forecasts"].values():
        assert set(f["runs"]) == {"BASE", "DELAYED_RECEIPT", "STRESS"}
        for v in f["variance"].values():
            assert v["variance"] == sum(v[k] for k in dashboard.CAUSES)


def test_dashboard_is_deterministic(run_dir, settings):
    first = (run_dir / "reports" / "dashboard.html").read_bytes()
    assert build_dashboard(run_dir, settings).read_bytes() == first


def test_payload_cannot_close_the_script_tag():
    html = render({"meta": {"scale": "x"}, "note": "</script><b>"})
    assert "</script><b>" not in html and _payload(html)["note"] == "</script><b>"


def test_dashboard_never_reads_ground_truth():
    assert "ground_truth" not in inspect.getsource(dashboard)
