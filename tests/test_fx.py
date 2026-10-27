import pandas as pd
import pytest

from liqfc.core import money
from liqfc.core.calendar import BusinessCalendar
from liqfc.engine.anomaly_detection import missing_rates
from liqfc.engine.fx_conversion import FxLookup, assert_single_currency, convert_to_reporting

CAL = BusinessCalendar(["2026-01-01"])
RATES = pd.DataFrame({
    "base_currency": ["EUR", "EUR", "EUR"],
    "rate_date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-08"]),
    "rate": ["1.10000000", "1.20000000", "1.30000000"],
})


def test_effective_date_rules():
    fx = FxLookup(RATES, CAL, "USD", max_fallback=2)
    assert fx.effective("EUR", "2026-01-06")[2] == "EXACT"
    r, d, s = fx.effective("EUR", "2026-01-07")
    assert s == "STALE_FALLBACK" and r == money.parse_rate("1.2") and str(d) == "2026-01-06"
    r, d, s = fx.effective("EUR", "2026-01-10")
    assert s == "STALE_FALLBACK" and r == money.parse_rate("1.3")
    assert fx.effective("USD", "2026-01-10") == (money.RATE_SCALE, None, "IDENTITY")
    assert fx.effective("EUR", "2026-01-02")[2] == "MISSING"


def test_weekend_uses_prior_business_day():
    rates = pd.concat([RATES, pd.DataFrame({"base_currency": ["EUR"], "rate_date": pd.to_datetime(["2026-01-09"]),
                                            "rate": ["1.40000000"]})])
    fx = FxLookup(rates, CAL, "USD")
    r, d, s = fx.effective("EUR", "2026-01-11")
    assert s == "PRIOR_BUSINESS_DAY" and r == money.parse_rate("1.4")


def test_missing_rate_never_becomes_one():
    out = convert_to_reporting([10000, 10000], ["EUR", "USD"], [None, None], {"EUR": 2, "USD": 2}, "USD")
    assert out == [None, 10000]


def test_conversion_precision_half_even():
    assert money.convert(5, 2, money.parse_rate("1.1"), 2) == 6
    assert money.convert(150, 0, money.parse_rate("0.0067"), 2) == 100


def test_detector_finds_missing_business_day_rates():
    acc = pd.DataFrame({"currency": ["EUR", "USD"]})
    m = missing_rates(acc, RATES, CAL, "2026-01-05", "2026-01-08", "USD")
    assert list(m["rate_id"]) == ["FX-2026-01-07-EUR-USD"]


def test_guard_refuses_mixed_currency_aggregation():
    df = pd.DataFrame({"k": [1, 1], "currency": ["USD", "EUR"]})
    with pytest.raises(ValueError):
        assert_single_currency(df, ["k"])


def test_injected_missing_rates_detected(injected, inj_result):
    _, gt = injected
    det = inj_result.detections
    assert set(det.loc[det["issue_type"] == "FX_RATE_MISSING", "affected_record_id"]) == \
        set(gt.loc[gt["issue_type"] == "FX_RATE_MISSING", "affected_record_id"])
    pos = inj_result.derived["daily_cash_position"]
    assert (pos["fx_rate_status"] == "STALE_FALLBACK").any()
    assert pos.loc[pos["fx_rate_status"] != "IDENTITY", "fx_rate"].notna().all()
