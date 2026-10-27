"""Regression tests for duplicate forecast items (audit finding H1 and the settled-original case)."""
import pandas as pd
import pytest

from liqfc.engine.anomaly_detection import detect_duplicates, duplicate_items
from liqfc.engine.forecast_run import candidate_lines
from liqfc.pipeline import build_exceptions, run_system
from liqfc.validation.reconciliation import input_business_checks

CUTOFF = pd.Timestamp("2026-04-01")


def _open_receipt(ds):
    cf = ds["cash_flow_forecast"]
    pick = cf[(cf["known_at"] < CUTOFF) & (cf["expected_date"] >= CUTOFF)
              & (cf["expected_date"] < CUTOFF + pd.Timedelta(days=30)) & (cf["status"] == "OPEN")
              & (cf["flow_category"] == "CUSTOMER_RECEIPT")]
    return pick.index[0]


def _with_duplicate(baseline, idx, new_id="CF-2026-999999", link=False, known_at=None):
    ds = baseline.copy()
    cf = ds["cash_flow_forecast"]
    dup = cf.loc[[idx]].copy()
    dup["cf_id"] = new_id
    if known_at is not None:
        dup["known_at"] = known_at
    ds["cash_flow_forecast"] = pd.concat([cf, dup], ignore_index=True)
    rev = ds["cash_flow_forecast_revision"]
    copy = rev[rev["cf_id"] == cf.at[idx, "cf_id"]].copy()
    copy["cf_id"] = new_id
    copy["revision_id"] = [f"CFR-2026-9{i:05d}" for i in range(len(copy))]
    if known_at is not None:
        copy.loc[copy["revision_no"] == 1, "known_at"] = known_at
    ds["cash_flow_forecast_revision"] = pd.concat([rev, copy], ignore_index=True)
    if link:
        links = ds["forecast_source_link"]
        row = links[links["cf_id"] == cf.at[idx, "cf_id"]].copy()
        row["link_id"], row["cf_id"] = "FSL-2026-999999", new_id
        ds["forecast_source_link"] = pd.concat([links, row], ignore_index=True)
    return ds


@pytest.fixture(scope="module")
def unlinked(baseline):
    idx = _open_receipt(baseline)
    return _with_duplicate(baseline, idx), baseline["cash_flow_forecast"].at[idx, "cf_id"]


def test_unlinked_duplicate_is_detected_with_high_severity(unlinked):
    ds, original = unlinked
    d = detect_duplicates(ds)
    assert list(d["affected_record_id"]) == ["CF-2026-999999"]
    row = d.iloc[0]
    assert row["rule_id"] == "DUP-001" and row["severity"] == "HIGH" and row["decision"] == "AUTO_RESOLVED"
    assert original in row["details"] and "no forecast_source_link row" in row["details"]


def test_unlinked_duplicate_is_excluded_from_the_run(unlinked, settings, calfx):
    ds, original = unlinked
    lines = candidate_lines(ds, settings, calfx[0], CUTOFF)
    dup = lines[lines["cf_id"] == "CF-2026-999999"]
    assert len(dup) == 1 and dup["exclusion_reason"].iloc[0] == "DUPLICATE_CASH_FLOW"
    assert lines.loc[lines["cf_id"] == original, "exclusion_reason"].isna().all()


def test_unlinked_duplicate_does_not_inflate_projected_cash(unlinked, settings, base_result):
    ds, _ = unlinked
    res = run_system(ds, settings)
    cols = ["forecast_run_id", "account_id", "forecast_date", "closing_minor", "inflows_minor", "outflows_minor"]
    pd.testing.assert_frame_equal(res.derived["forecast_position"][cols], base_result.derived["forecast_position"][cols])
    exc = build_exceptions(res, pd.DataFrame(columns=["rule_id", "issue_type", "affected_table", "affected_record_id",
                                                      "severity", "observed_value", "expected_value", "decision",
                                                      "details"]), settings)
    dup_exc = exc[exc["affected_record_id"] == "CF-2026-999999"]
    assert set(dup_exc["source"]) == {"DETECTION", "FORECAST_GUARD"}
    assert (dup_exc["issue_type"] == "DUPLICATE_CASH_FLOW").all()


def test_missing_source_link_fails_validation(unlinked, baseline, settings, calfx):
    ds, _ = unlinked
    checks = {c.check_id: c for c in input_business_checks(ds.tables, settings, calfx[0])}
    link = checks["BUS-CF-LINK"]
    assert link.status == "FAIL" and [f[0] for f in link.failures] == ["CF-2026-999999"]
    assert link.emit_exceptions and link.severity == "HIGH"
    assert checks["BUS-DUPITEM"].status == "FAIL"
    clean = {c.check_id: c for c in input_business_checks(baseline.tables, settings, calfx[0])}
    assert clean["BUS-CF-LINK"].status == "PASS" and clean["BUS-DUPITEM"].status == "PASS"


def test_link_pointing_to_another_source_fails_validation(baseline, settings, calfx):
    ds = baseline.copy()
    links = ds["forecast_source_link"].copy()
    other = links["source_id"].iloc[-1]
    links.loc[0, "source_id"] = other
    ds["forecast_source_link"] = links
    link = {c.check_id: c for c in input_business_checks(ds.tables, settings, calfx[0])}["BUS-CF-LINK"]
    assert link.status == "FAIL" and links.loc[0, "cf_id"] in [f[0] for f in link.failures]


def test_labelled_duplicates_never_included_even_after_original_settles(injected, inj_result):
    _, gt = injected
    dups = set(gt.loc[gt["issue_type"] == "DUPLICATE_CASH_FLOW", "affected_record_id"])
    lines = inj_result.derived["forecast_run_line"]
    dup_lines = lines[lines["cf_id"].isin(dups)]
    assert len(dup_lines) > 0
    assert (dup_lines["line_status"] == "EXCLUDED").all()
    assert (dup_lines["exclusion_reason"] == "DUPLICATE_CASH_FLOW").all()


def test_settled_original_survives_a_duplicate_known_earlier(baseline):
    cf = baseline["cash_flow_forecast"]
    act = baseline["actual_cash_flow"]
    idx = cf.index[cf["cf_id"].isin(set(act["cf_id"])) & (cf["flow_category"] == "CUSTOMER_RECEIPT")][0]
    earlier = cf.at[idx, "known_at"] - pd.Timedelta(days=1)
    ds = _with_duplicate(baseline, idx, known_at=earlier, link=True)
    d = duplicate_items(ds["cash_flow_forecast"], ds["forecast_source_link"], set(ds["actual_cash_flow"]["cf_id"]))
    assert list(d["cf_id"]) == ["CF-2026-999999"]
    assert d["kept_cf_id"].iloc[0] == cf.at[idx, "cf_id"]


def test_instalments_and_same_amount_items_are_not_duplicates(baseline):
    cf = baseline["cash_flow_forecast"]
    assert duplicate_items(cf, baseline["forecast_source_link"], set(baseline["actual_cash_flow"]["cf_id"])).empty
    assert (cf.groupby(["source_type", "source_id"]).size() == 2).any()
    assert cf.duplicated(["account_id", "amount_minor", "expected_date"], keep=False).sum() >= 2
