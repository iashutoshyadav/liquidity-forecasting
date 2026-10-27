import numpy as np
import pandas as pd

from liqfc.engine.balances import account_daily_balances


def test_clean_baseline_reconciles(base_result):
    pos = base_result.derived["daily_cash_position"]
    assert (pos["reconciliation_status"] != "BREAK").all()
    assert (pos["reconciliation_difference_minor"] == pos["explained_difference_minor"]).all()
    zero = pos["reconciliation_difference_minor"] == 0
    assert (pos.loc[zero, "reconciliation_status"] == "RECONCILED").all()
    assert (pos.loc[~zero, "reconciliation_status"] == "RECONCILING_ITEM").all() and (~zero).any()


def test_balance_equation_and_continuity(base_result):
    p = base_result.derived["daily_cash_position"].sort_values(["account_id", "position_date"])
    assert (p["opening_balance_minor"] + p["credits_minor"] - p["debits_minor"] == p["closing_balance_minor"]).all()
    prev = p.groupby("account_id")["closing_balance_minor"].shift(1)
    assert (prev.dropna() == p.loc[prev.notna(), "opening_balance_minor"]).all()


def test_balance_hand_calculation():
    acc = pd.DataFrame({"account_id": ["A"], "currency": ["USD"], "entity_id": ["E"], "opening_balance_minor": [1000]})
    led = pd.DataFrame({"account_id": ["A", "A", "A"], "currency": ["USD"] * 3, "amount_minor": [500, -200, -50],
                        "value_date": pd.to_datetime(["2026-01-02", "2026-01-02", "2026-01-04"])})
    b = account_daily_balances(acc, led, "2026-01-01", "2026-01-04").set_index("position_date")
    assert list(b["closing_balance_minor"]) == [1000, 1300, 1300, 1250]
    assert b.loc["2026-01-02", "credits_minor"] == 500 and b.loc["2026-01-02", "debits_minor"] == 200


def test_mixed_currency_ledger_is_never_summed():
    acc = pd.DataFrame({"account_id": ["A"], "currency": ["USD"], "entity_id": ["E"], "opening_balance_minor": [0]})
    led = pd.DataFrame({"account_id": ["A", "A"], "currency": ["EUR", "USD"], "amount_minor": [5, 7],
                        "value_date": pd.to_datetime(["2026-01-02", "2026-01-02"])})
    b = account_daily_balances(acc, led, "2026-01-01", "2026-01-03").set_index("position_date")
    assert list(b["closing_balance_minor"]) == [0, 7, 7]
    assert b.loc["2026-01-02", "credits_minor"] == 7 and b.loc["2026-01-02", "entry_count"] == 1


def test_consolidation_sums(base_result):
    p = base_result.derived["daily_cash_position"]
    c = base_result.derived["consolidated_cash_position"]
    ent = p.groupby(["entity_id", "position_date"])["closing_balance_reporting_minor"].sum().astype(np.int64)
    ce = c[c["level"] == "ENTITY"].set_index(["scope_id", "position_date"])["closing_balance_reporting_minor"]
    assert (ent.reindex(ce.index) == ce).all()
    g = c[c["level"] == "GROUP"].set_index("position_date")["closing_balance_reporting_minor"]
    assert (ce.groupby(level=1).sum() == g).all()


def test_injected_breaks_detected_exactly(injected, inj_result):
    _, gt = injected
    pos = inj_result.derived["daily_cash_position"]
    st = injected[0]["account_balance_statement"]
    brk = pos[pos["reconciliation_status"] == "BREAK"].merge(
        st, left_on=["account_id", "position_date"], right_on=["account_id", "statement_date"])
    labelled = set(gt.loc[gt["issue_type"] == "BALANCE_MISMATCH", "affected_record_id"])
    assert set(brk["statement_id"]) == labelled
