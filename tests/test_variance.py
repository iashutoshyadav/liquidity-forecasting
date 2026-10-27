def test_local_components_sum_exactly(base_result, inj_result):
    for res in (base_result, inj_result):
        c = res.derived["forecast_comparison"]
        assert (c["variance_minor"] == c["actual_closing_minor"] - c["forecast_closing_minor"]).all()
        assert (c["variance_minor"] == c[["timing_minor", "amount_minor", "unforecast_minor", "anomaly_minor"]].sum(axis=1)).all()


def test_reporting_residual_within_tolerance(base_result, settings):
    c = base_result.derived["forecast_comparison"].dropna(subset=["variance_reporting_minor"])
    tol = settings.raw["money"]["variance_rounding_tolerance_minor"]
    assert c["residual_reporting_minor"].abs().max() <= tol
    comps = ["timing_reporting_minor", "amount_reporting_minor", "unforecast_reporting_minor", "anomaly_reporting_minor",
             "fx_movement_reporting_minor", "residual_reporting_minor"]
    assert (c["variance_reporting_minor"] == c[comps].sum(axis=1)).all()


def test_attribution_detail_reconciles_to_comparison(base_result):
    c = base_result.derived["forecast_comparison"]
    att = base_result.derived["variance_attribution"]
    runs = base_result.derived["forecast_run"].set_index("forecast_run_id")
    end = c[c["position_date"] == c["forecast_run_id"].map(runs["horizon_end_date"])]
    tot = att.groupby(["forecast_run_id", "account_id", "category"])["amount_minor"].sum().unstack(fill_value=0)
    for r in end.itertuples(index=False):
        key = (r.forecast_run_id, r.account_id)
        row = tot.loc[key] if key in tot.index else {}
        assert row.get("TIMING", 0) == r.timing_minor and row.get("AMOUNT", 0) == r.amount_minor
        assert row.get("UNFORECAST", 0) == r.unforecast_minor and row.get("ANOMALY", 0) == r.anomaly_minor


def test_every_explanation_has_source_reference(base_result):
    att = base_result.derived["variance_attribution"]
    assert att["source_id"].notna().all() and (att["source_id"].str.len() > 0).all()
    assert set(att["category"]) <= {"TIMING", "AMOUNT", "UNFORECAST", "ANOMALY"}
    assert (att["source_count"] >= 1).all()


def test_no_anomaly_variance_on_clean_baseline(base_result):
    assert (base_result.derived["forecast_comparison"]["anomaly_minor"] == 0).all()


def test_anomalous_items_attributed_to_anomaly(injected, inj_result):
    att = inj_result.derived["variance_attribution"]
    assert (att["category"] == "ANOMALY").any()
    flagged = set(inj_result.detections["affected_record_id"])
    act = injected[0]["actual_cash_flow"].set_index("ledger_entry_id")["cf_id"]
    ano = att[att["category"] == "ANOMALY"]
    assert ano["source_id"].map(act).isin(flagged).all()


def test_comparisons_only_on_matching_account_dates(base_result, settings):
    c = base_result.derived["forecast_comparison"]
    assert c["position_date"].max() <= settings.end
    assert not c.duplicated(["forecast_run_id", "account_id", "position_date"]).any()
