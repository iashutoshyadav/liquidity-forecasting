"""Regression tests for the independent bank-side model and reconciliation (audit M4)."""
import inspect

import pandas as pd

from liqfc.engine import bank_reconciliation
from liqfc.engine.bank_reconciliation import match_items
from liqfc.generators import balance_statements
from liqfc.pipeline import run_system


def _items(base_result):
    return base_result.derived["bank_reconciliation_item"]


def test_bank_model_never_reads_ledger_rows():
    src = inspect.getsource(balance_statements)
    assert "ledger_entry" not in src and "ledger_entry_id" not in src


def test_matched_items_pair_same_account_and_amount(baseline, base_result):
    it = _items(base_result)
    m = it[it["match_status"] == "MATCHED"]
    assert len(m) > 0.9 * len(it)
    led = baseline["ledger_entry"].set_index("ledger_entry_id")
    bl = baseline["bank_statement_line"].set_index("bank_line_id")
    assert (m["ledger_entry_id"].map(led["amount_minor"]).to_numpy() == m["bank_line_id"].map(bl["amount_minor"]).to_numpy()).all()
    assert (m["ledger_date"] == m["bank_date"]).all()


def test_timing_differences_are_explained_not_breaks(base_result, settings):
    it = _items(base_result)
    t = it[it["match_status"] == "TIMING_DIFFERENCE"]
    assert len(t) > 0 and (t["bank_date"] > t["ledger_date"]).all()
    pos = base_result.derived["daily_cash_position"].set_index(["account_id", "position_date"])
    r = t.iloc[0]
    day = pos.loc[(r["account_id"], r["ledger_date"])]
    assert day["reconciliation_status"] == "RECONCILING_ITEM" and day["explained_difference_minor"] != 0
    assert (base_result.derived["daily_cash_position"]["reconciliation_status"] != "BREAK").all()


def test_bank_only_fees_are_unmatched_and_absent_from_ledger(baseline, base_result):
    it = _items(base_result)
    bo = it[it["match_status"] == "BANK_ONLY"]
    bl = baseline["bank_statement_line"].set_index("bank_line_id")
    fees = bl[bl["description"] == "Wire transfer charge"]
    assert len(fees) > 0 and set(fees.index) <= set(bo["bank_line_id"])
    assert not baseline["ledger_entry"]["description"].eq("Wire transfer charge").any()


def test_ledger_only_items_are_receipts_clearing_after_data_end(baseline, base_result, settings):
    it = _items(base_result)
    lo = it[it["match_status"] == "LEDGER_ONLY"]
    if len(lo):
        assert (lo["ledger_date"] >= settings.end - pd.Timedelta(days=7)).all()


def test_unexplained_statement_change_is_a_break(baseline, settings):
    ds = baseline.copy()
    st = ds["account_balance_statement"].copy()
    i = st.index[st["statement_date"] == pd.Timestamp("2026-02-17")][0]
    st.at[i, "closing_balance_minor"] = int(st.at[i, "closing_balance_minor"]) + 12345
    ds["account_balance_statement"] = st
    res = run_system(ds, settings)
    pos = res.derived["daily_cash_position"]
    brk = pos[pos["reconciliation_status"] == "BREAK"]
    assert list(zip(brk["account_id"], brk["position_date"])) == [(st.at[i, "account_id"], pd.Timestamp("2026-02-17"))]
    assert (res.detections["rule_id"] == "BAL-001").sum() == 1


def test_ledger_change_does_not_move_the_bank_statement(baseline, settings):
    """Independence: editing a ledger amount leaves bank lines alone, so reconciliation breaks."""
    ds = baseline.copy()
    led = ds["ledger_entry"].copy()
    j = led.index[(led["value_date"] == pd.Timestamp("2026-03-10")) & led["entry_type"].eq("SUNDRY_PAYMENT")][0]
    led.at[j, "amount_minor"] = int(led.at[j, "amount_minor"]) - 500
    ds["ledger_entry"] = led
    res = run_system(ds, settings)
    pos = res.derived["daily_cash_position"]
    acct = led.at[j, "account_id"]
    p = pos[(pos["account_id"] == acct) & (pos["position_date"] >= pd.Timestamp("2026-03-10"))]
    assert (p["reconciliation_status"] == "BREAK").all()
    it = res.derived["bank_reconciliation_item"]
    assert led.at[j, "ledger_entry_id"] in set(it.loc[it["match_status"] == "UNMATCHED_LEDGER", "ledger_entry_id"])
    pd.testing.assert_frame_equal(ds["bank_statement_line"], baseline["bank_statement_line"])


def test_matching_is_one_to_one_and_respects_window(calfx):
    cal = calfx[0]
    led = pd.DataFrame({"ledger_entry_id": ["L1", "L2", "L3"], "account_id": ["A", "A", "A"],
                        "value_date": pd.to_datetime(["2026-03-02", "2026-03-02", "2026-03-02"]), "amount_minor": [100, 100, 7]})
    bank = pd.DataFrame({"bank_line_id": ["B1", "B2", "B3"], "account_id": ["A", "A", "A"],
                         "booking_date": pd.to_datetime(["2026-03-02", "2026-03-04", "2026-03-12"]), "amount_minor": [100, 100, 7]})
    bank["bank_tx_code"] = ["RECEIPT", "RECEIPT", "RECEIPT"]
    it = match_items(led, bank, cal, 3, 2026, data_end="2026-06-30").set_index(["ledger_entry_id"], drop=False)
    assert it.loc["L1", "match_status"] == "MATCHED" and it.loc["L2", "match_status"] == "TIMING_DIFFERENCE"
    assert it.loc["L3", "match_status"] == "UNMATCHED_LEDGER"
    assert set(it.loc[it["match_status"] == "UNMATCHED_BANK", "bank_line_id"]) == {"B3"}
    late = match_items(led.assign(value_date=pd.Timestamp("2026-06-29")), bank.iloc[0:0], cal, 3, 2026, data_end="2026-06-30")
    assert set(late["match_status"]) == {"LEDGER_ONLY"}


def test_reconciliation_engine_documents_rules():
    assert "REC-03" in inspect.getsource(bank_reconciliation)
