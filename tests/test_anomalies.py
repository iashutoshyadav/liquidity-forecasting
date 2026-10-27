import inspect

import pandas as pd

from liqfc.core.schema import INPUT_TABLES, SCHEMA
from liqfc.engine import anomaly_detection
from liqfc.engine.anomaly_detection import detect_all, detect_duplicates, detect_mixed_currency
from liqfc.engine.evaluation import detection_metrics


def test_every_anomaly_type_injected(injected, settings):
    _, gt = injected
    counts = gt["issue_type"].value_counts().to_dict()
    assert counts == settings.anomaly_counts
    assert gt["anomaly_id"].is_unique and gt["affected_record_id"].is_unique


def test_detection_matches_ground_truth(injected, inj_result):
    _, gt = injected
    m = detection_metrics(inj_result.detections, gt)
    for t, v in m.items():
        assert v["fp"] == 0 and v["fn"] == 0, (t, v)


def test_no_false_positives_on_clean_baseline(base_result):
    assert len(base_result.detections) == 0


def test_injectors_only_touch_targets(baseline, injected):
    inj, gt = injected
    targets = set(gt["affected_record_id"])
    for name in INPUT_TABLES:
        pk = SCHEMA[name].pk[0]
        a = baseline[name].set_index(pk)
        b = inj[name].set_index(pk)
        removed = set(a.index) - set(b.index)
        added = set(b.index) - set(a.index)
        common = a.index.intersection(b.index)
        changed = common[~(a.loc[common].fillna("<NA>").astype(str) == b.loc[common].fillna("<NA>").astype(str)).all(axis=1)]
        allowed_added = targets | set(gt.loc[gt["issue_type"] == "DUPLICATE_CASH_FLOW", "injected_value"])
        if name == "forecast_source_link":
            assert set(b.loc[list(added), "cf_id"]) <= allowed_added
        else:
            assert added <= allowed_added, name
        assert removed <= targets, name
        assert set(changed) <= targets, (name, list(changed)[:5])


def test_original_and_injected_values_recorded(injected):
    _, gt = injected
    bm = gt[gt["issue_type"] == "BALANCE_MISMATCH"]
    assert (bm["original_value"] != bm["injected_value"]).all()
    assert (gt["expected_detection_rule"].str.len() > 0).all()
    assert set(gt["injection_stage"]) == {"POST_BASELINE_VALIDATION"}


def test_legitimate_same_amount_items_not_duplicates(baseline):
    cf = baseline["cash_flow_forecast"]
    same = cf[cf.duplicated(["account_id", "amount_minor", "expected_date"], keep=False)]
    assert len(same) >= 2
    assert detect_duplicates(baseline).empty
    inst = baseline["forecast_source_link"].groupby("source_id").size()
    assert (inst > 1).any()


def test_duplicate_detector_flags_later_inclusion(injected):
    inj, gt = injected
    d = detect_duplicates(inj)
    dup_gt = gt[gt["issue_type"] == "DUPLICATE_CASH_FLOW"]
    assert set(d["affected_record_id"]) == set(dup_gt["injected_value"])
    assert not set(d["affected_record_id"]) & set(dup_gt["original_value"])


def test_mixed_currency_detected(injected):
    inj, gt = injected
    d = detect_mixed_currency(inj)
    assert set(d["affected_record_id"]) == set(gt.loc[gt["issue_type"] == "MIXED_CURRENCY", "affected_record_id"])


def test_ground_truth_isolation(injected, inj_result, settings, calfx):
    inj, gt = injected
    src = inspect.getsource(anomaly_detection)
    assert "ground_truth" not in src and "anomalies" not in src.split('"""', 2)[2]
    for name in inj.tables:
        assert not {"issue_type", "anomaly_id", "expected_decision"} & set(inj[name].columns)
    assert "ground_truth" not in inj.tables
    again = detect_all(inj, inj_result.positions_raw, calfx[0], settings)
    pd.testing.assert_frame_equal(again.reset_index(drop=True), inj_result.detections.reset_index(drop=True))
