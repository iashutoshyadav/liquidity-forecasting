"""Regression tests for the forecast-item revision history and look-ahead detection (audit M2)."""
import pandas as pd
import pytest

from liqfc.engine.anomaly_detection import detect_cutoff_leakage
from liqfc.engine.forecast_run import candidate_lines, items_as_of
from liqfc.pipeline import run_system
from liqfc.validation.reconciliation import input_business_checks


def _revised_item(baseline, cutoff_between=True):
    """An item with a CUSTOMER_PROMISE revision and a cutoff before (or after) that revision."""
    rev = baseline["cash_flow_forecast_revision"]
    r2 = rev[rev["reason"] == "CUSTOMER_PROMISE"].sort_values("known_at")
    cuts = [pd.Timestamp(c) for c in ("2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01")]
    for r in r2.itertuples(index=False):
        r1 = rev[(rev["cf_id"] == r.cf_id) & (rev["revision_no"] == 1)].iloc[0]
        for c in cuts:
            if cutoff_between and r1.known_at < c <= r.known_at and min(r1.expected_date, r.expected_date) >= c:
                return r.cf_id, c, r1, r
            if not cutoff_between and r.known_at < c and r.expected_date >= c and r.expected_date < c + pd.Timedelta(days=30):
                return r.cf_id, c, r1, r
    pytest.skip("no suitable revised item in starter data")


def test_revision_history_is_complete_and_consistent(baseline, settings, calfx):
    cf, rev = baseline["cash_flow_forecast"], baseline["cash_flow_forecast_revision"]
    first = rev[rev["revision_no"] == 1].set_index("cf_id")
    assert set(first.index) == set(cf["cf_id"])
    assert (cf.set_index("cf_id")["known_at"] == first.loc[cf["cf_id"], "known_at"].values).all()
    assert (rev["reason"] == "CUSTOMER_PROMISE").sum() > 0
    checks = {c.check_id: c for c in input_business_checks(baseline.tables, settings, calfx[0])}
    assert checks["BUS-REV"].status == "PASS", checks["BUS-REV"].failures[:3]


def test_promise_revisions_are_recorded_before_settlement(baseline):
    rev, act = baseline["cash_flow_forecast_revision"], baseline["actual_cash_flow"]
    first_rec = act.groupby("cf_id")["recorded_at"].min()
    item_known = rev[rev["revision_no"] == 1].set_index("cf_id")["known_at"]
    p = rev[rev["reason"] == "CUSTOMER_PROMISE"]
    assert (p["known_at"] > p["cf_id"].map(item_known)).all()
    settled = p[p["cf_id"].isin(first_rec.index)]
    assert (settled["known_at"] < settled["cf_id"].map(first_rec)).all()


def test_run_uses_version_known_before_cutoff(baseline, settings, calfx):
    cid, cut, r1, r2 = _revised_item(baseline, cutoff_between=True)
    lines = candidate_lines(baseline, settings, calfx[0], cut)
    line = lines[lines["cf_id"] == cid]
    if len(line):
        assert line["revision_id"].iloc[0] == r1.revision_id
    snap = items_as_of(baseline["cash_flow_forecast"], baseline["cash_flow_forecast_revision"], cut).set_index("cf_id")
    assert snap.at[cid, "revision_id"] == r1.revision_id and snap.at[cid, "expected_date"] == r1.expected_date


def test_run_uses_revision_recorded_before_cutoff(baseline, settings, calfx):
    cid, cut, r1, r2 = _revised_item(baseline, cutoff_between=False)
    snap = items_as_of(baseline["cash_flow_forecast"], baseline["cash_flow_forecast_revision"], cut).set_index("cf_id")
    assert snap.at[cid, "revision_id"] == r2.revision_id and snap.at[cid, "expected_date"] == r2.expected_date


def test_future_revision_cannot_change_a_past_forecast(baseline, settings, base_result):
    ds = baseline.copy()
    rev = ds["cash_flow_forecast_revision"]
    cf = ds["cash_flow_forecast"]
    target = cf[(cf["expected_date"] >= pd.Timestamp("2026-04-01")) & (cf["expected_date"] < pd.Timestamp("2026-04-30"))
                & (cf["status"] == "SETTLED")].iloc[0]
    late = rev[rev["cf_id"] == target["cf_id"]].sort_values("revision_no").iloc[[-1]].copy()
    late["revision_id"], late["revision_no"] = "CFR-2026-999999", late["revision_no"] + 1
    late["known_at"] = pd.Timestamp("2026-04-15 10:00")
    late["expected_date"], late["amount_minor"] = pd.Timestamp("2026-04-20"), late["amount_minor"] * 3
    late["reason"] = "REESTIMATE"
    ds["cash_flow_forecast_revision"] = pd.concat([rev, late], ignore_index=True)
    res = run_system(ds, settings)
    runs = base_result.derived["forecast_run"]
    early = set(runs.loc[runs["cutoff_date"] <= pd.Timestamp("2026-04-01"), "forecast_run_id"])
    cols = ["forecast_run_id", "account_id", "forecast_date", "closing_minor"]
    a = base_result.derived["forecast_position"]
    b = res.derived["forecast_position"]
    pd.testing.assert_frame_equal(a[a["forecast_run_id"].isin(early)][cols].reset_index(drop=True),
                                  b[b["forecast_run_id"].isin(early)][cols].reset_index(drop=True))


def test_backdated_revision_with_later_basis_is_detected_and_excluded(baseline, settings, calfx):
    ds = baseline.copy()
    cf, rev, act = ds["cash_flow_forecast"], ds["cash_flow_forecast_revision"], ds["actual_cash_flow"]
    cut = pd.Timestamp("2026-04-01")
    first = act.sort_values("recorded_at").drop_duplicates("cf_id").set_index("cf_id")
    cand = cf[cf["cf_id"].isin(first.index) & (cf["known_at"] < cut) & (cf["flow_category"] == "CUSTOMER_RECEIPT")
              & (cf["expected_date"] >= cut) & (cf["expected_date"] < cut + pd.Timedelta(days=30))
              & cf["cf_id"].map(first["recorded_at"]).ge(cut) & ~cf["cf_id"].isin(rev.loc[rev["revision_no"] > 1, "cf_id"])]
    item = cand.iloc[0]
    a = first.loc[item["cf_id"]]
    new = {"revision_id": "CFR-2026-999998", "cf_id": item["cf_id"], "revision_no": 2,
           "known_at": item["known_at"] + pd.Timedelta(hours=1), "expected_date": a["actual_date"],
           "amount_minor": int(a["amount_minor"]), "currency": item["currency"], "basis_actual_id": a["actual_id"],
           "reason": "REESTIMATE"}
    ds["cash_flow_forecast_revision"] = pd.concat([rev, pd.DataFrame([new])], ignore_index=True)
    i = cf.index[cf["cf_id"] == item["cf_id"]][0]
    cf.loc[i, ["expected_date", "amount_minor", "basis_actual_id"]] = [a["actual_date"], int(a["amount_minor"]), a["actual_id"]]
    det = detect_cutoff_leakage(ds)
    assert item["cf_id"] in set(det.loc[det["rule_id"] == "CUT-001", "affected_record_id"])
    lines = candidate_lines(ds, settings, calfx[0], cut)
    assert (lines.loc[lines["cf_id"] == item["cf_id"], "exclusion_reason"] == "CUTOFF_LEAKAGE").all()


def test_untracked_overwrite_is_flagged_and_ignored_by_forecasts(baseline, settings, calfx):
    ds = baseline.copy()
    cf, act = ds["cash_flow_forecast"].copy(), ds["actual_cash_flow"]
    a = act.drop_duplicates("cf_id").iloc[100]
    i = cf.index[cf["cf_id"] == a["cf_id"]][0]
    cut = pd.Timestamp("2026-04-01")
    before = candidate_lines(baseline, settings, calfx[0], cut)
    cf.at[i, "expected_date"] = a["actual_date"] + pd.Timedelta(days=1)
    ds["cash_flow_forecast"] = cf
    det = detect_cutoff_leakage(ds)
    hit = det[det["rule_id"] == "CUT-003"]
    assert list(hit["affected_record_id"]) == [a["cf_id"]] and hit["decision"].iloc[0] == "HELD_FOR_REVIEW"
    pd.testing.assert_frame_equal(before, candidate_lines(ds, settings, calfx[0], cut))


def test_no_false_positives_on_legitimate_revisions(baseline):
    assert detect_cutoff_leakage(baseline).empty
