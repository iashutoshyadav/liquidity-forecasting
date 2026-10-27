import pandas as pd

from liqfc.core import ids


def test_formats_match_conventions():
    assert ids.entity_id(1) == "ENT-000001"
    assert ids.account_id("ENT-000001", 1) == "BANKACC-ENT-000001-001"
    assert ids.ledger_id(2026, 1) == "LED-2026-00000001"
    assert ids.seq_id("CF", 2026, 1) == "CF-2026-000001"
    assert ids.run_id(pd.Timestamp("2026-01-01"), 1) == "FCAST-2026-01-01-001"
    assert ids.rate_id(pd.Timestamp("2026-03-02"), "EUR", "USD") == "FX-2026-03-02-EUR-USD"


def test_patterns_accept_and_reject():
    assert ids.matches("entity", "ENT-000123")
    assert not ids.matches("entity", "ENT-123")
    assert ids.matches("account", "BANKACC-ENT-000001-007")
    assert not ids.matches("account", "BANKACC-ENT-000001-7")
    assert ids.matches("ledger", "LED-2026-00000001")
    assert not ids.matches("ledger", "LED-2026-0000001")
    assert ids.matches("run", "FCAST-2026-06-01-004")


def test_sequences_unique_and_zero_padded():
    seq = ids.seq_ids("CF", 2026, 20000)
    assert len(set(seq)) == 20000
    assert seq[0] == "CF-2026-000001" and seq[-1] == "CF-2026-020000"
