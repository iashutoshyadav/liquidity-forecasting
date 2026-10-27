# Scenario assumptions

Scenarios are defined in `config/scenarios.yaml` and stored in `liquidity_scenario` and
`scenario_assumption`. A scenario can change a forecast **only** through these five parameters.
Every line it changes records the assumption IDs that changed it (`forecast_run_line.assumption_ids`).

| Assumption | BASE | DELAYED_RECEIPT | STRESS |
|---|---|---|---|
| `ASM-*-01` receivable_delay_proportion | 0 | 0.30 | 0.40 |
| `ASM-*-02` receivable_delay_days | 0 | 14 | 21 |
| `ASM-*-03` collection_haircut_bp | 0 | 0 | 1,000 (10%) |
| `ASM-*-04` fx_shock_bp | none | none | EUR −500, GBP −500, INR −800, JPY −600 |
| `ASM-*-05` one_off_payment | none | none | 8,000,000.00 USD from ENT-000001's OPERATING account, cutoff + 7 days (next business day) |

## Transformation (applied to the BASE candidate lines of the same cutoff)

1. **Delay**: only CUSTOMER_RECEIPT lines. A line is selected when `sha256(f"{seed}|{scenario_code}|{cf_id}")[:8] / 2^64 < proportion`. This is stable per item, independent of row order, and identical across cutoffs. adjusted_date = next business day on/after base_date + delay_days. A line that moves past the horizon end becomes OUT_OF_HORIZON.
2. **Haircut**: every CUSTOMER_RECEIPT line, adjusted_amount = half_even(amount × (10,000 − bp) / 10,000).
3. **FX shock**: applied to the cutoff rate used for reporting-currency conversion. The shocked rate is stored per account (`forecast_run_account.fx_rate_applied`).
4. **One-off payment**: an extra line with `source_type = SCENARIO` and no `cf_id`. Its local amount = 8,000,000.00 USD / unshocked cutoff rate.

Intercompany receipts, payables, payroll, rent, tax and opening balances are identical across scenarios.

## Verification

* `tests/test_scenarios.py` re-derives every DELAYED_RECEIPT and STRESS line from the BASE run with an independent implementation of the formula above, and checks the FX shock and the one-off line.
* Validation check `BUS-SCN` repeats this inside every pipeline run and is reported in `validation_summary.json`.
* Within one cutoff, BASE ≥ DELAYED_RECEIPT ≥ STRESS for minimum group cash (also tested).
