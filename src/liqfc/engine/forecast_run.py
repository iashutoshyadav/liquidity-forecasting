"""Versioned forecast runs with strict historical cutoff.

FC-01 cutoff_ts = cutoff_date 00:00Z. Only records with recorded_at/known_at < cutoff_ts are used.
FC-02 opening balance = account opening + sum(ledger entries recorded before cutoff), excluding
      entries quarantined for being in the wrong currency (BAL-02).
FC-03 candidate items: known before cutoff, not cancelled/written off before cutoff, remaining
      amount = amount - settlements recorded before cutoff (payroll: any settlement closes it).
FC-04 overdue unsettled items are re-dated: receivables cutoff + overdue_receivable_roll_days,
      payables cutoff + overdue_payable_roll_days (next business day).
FC-05 guards (excluded, logged): CUTOFF_LEAKAGE (basis recorded >= cutoff), MIXED_CURRENCY
      (item currency != account currency), DUPLICATE_CASH_FLOW (source allocation > 100% or a
      second item for the same source instalment, with or without a source link). Duplicates are
      judged against every item known before cutoff, including items already settled, and the
      item holding settlements recorded before cutoff is kept.
FC-06 scenario transformation = engine/scenarios.apply_scenario (documented assumptions only).
FC-07 FX: last rate published before cutoff (effective for cutoff-1 day) x (1 + shock_bp/10000).
FC-08 a re-run creates a new run id (next sequence for the cutoff) and supersedes the old one.
FC-10 cash-pool sweeps are projected with the same weekly rule as the actual sweeps (sweep_lines).
FC-11 item values (expected date, amount, currency, basis) come from the latest revision known before
      cutoff (items_as_of); later revisions cannot change an earlier run.
FC-12 recurring flows with no forecast item yet are estimated from activity recorded before cutoff
      (run_rate_lines): payroll not yet booked = last payroll actual on the next pay day; small flows
      (sundry, bank fees) = each account's net per business day over the lookback, on every business
      day of the horizon. The same lines are used in every scenario.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from liqfc.core import ids, money
from liqfc.core.dataset import Dataset
from liqfc.core.enums import SCENARIO_CODES
from liqfc.engine.anomaly_detection import basis_leakage, currency_mismatch, duplicate_items
from liqfc.engine.balances import split_ledger_by_currency
from liqfc.engine.fx_conversion import FxLookup, convert_to_reporting, rate_text
from liqfc.engine.scenarios import apply_scenario, fx_shock_bp, one_off_line


@dataclass
class RunSpec:
    run_id: str
    cutoff: pd.Timestamp
    code: str
    scenario_id: str
    order: int
    supersedes: str | None = None
    reason: str | None = None
    actor: str = "OPERATIONS_ANALYST"


def plan_runs(settings) -> list[RunSpec]:
    specs, order = [], 0
    seq: dict[pd.Timestamp, int] = {}
    by_key = {}
    for c in sorted(pd.Timestamp(x) for x in settings.forecast["cutoff_dates"]):
        for code in SCENARIO_CODES:
            seq[c] = seq.get(c, 0) + 1
            spec = RunSpec(ids.run_id(c, seq[c]), c, code, settings.scenarios[code]["scenario_id"], order)
            specs.append(spec)
            by_key[(c, code)] = spec
            order += 1
    for r in settings.forecast.get("reruns", []):
        c = pd.Timestamp(r["cutoff_date"])
        old = by_key[(c, r["scenario_code"])]
        seq[c] += 1
        specs.append(RunSpec(ids.run_id(c, seq[c]), c, r["scenario_code"], old.scenario_id, order,
                             supersedes=old.run_id, reason=r["reason"], actor=r.get("actor_role", "RISK_REVIEWER")))
        order += 1
    return specs


VALUE_FIELDS = ["expected_date", "amount_minor", "currency", "basis_actual_id"]


def items_as_of(cf: pd.DataFrame, revisions: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    """FC-11: each item as it was known strictly before ``ts`` (latest revision with known_at < ts).

    Static fields (account, source, category, status) come from the item; value fields come from the
    revision. Items with no revision known before ``ts`` are not visible. Adds revision_id and
    revision_known_at.
    """
    r = revisions[revisions["known_at"] < ts].sort_values(["cf_id", "revision_no"], kind="mergesort")
    r = r.drop_duplicates("cf_id", keep="last")
    r = r[["cf_id", "revision_id", "known_at"] + VALUE_FIELDS].rename(columns={"known_at": "revision_known_at"})
    return cf.drop(columns=VALUE_FIELDS).merge(r, on="cf_id", how="inner")


def candidate_lines(ds: Dataset, settings, cal, cutoff: pd.Timestamp) -> pd.DataFrame:
    ts = pd.Timestamp(cutoff)
    fc = settings.forecast
    cf = ds["cash_flow_forecast"]
    visible = items_as_of(cf, ds["cash_flow_forecast_revision"], ts)
    known = visible
    pse, rse = ds["payable_status_event"], ds["receivable_status_event"]
    closed = set(pse.loc[(pse["to_status"] == "CANCELLED") & (pse["recorded_at"] < ts), "payable_id"]) | \
        set(rse.loc[(rse["to_status"] == "WRITTEN_OFF") & (rse["recorded_at"] < ts), "receivable_id"])
    known = known[~known["source_id"].isin(closed)].copy()

    act = ds["actual_cash_flow"]
    settled = act[act["recorded_at"] < ts].groupby("cf_id")["amount_minor"].sum()
    known["settled_before_cutoff_minor"] = known["cf_id"].map(settled).fillna(0).astype(np.int64)
    known["remaining"] = known["amount_minor"].astype(np.int64) - known["settled_before_cutoff_minor"]
    known.loc[(known["flow_category"] == "PAYROLL") & (known["settled_before_cutoff_minor"] != 0), "remaining"] = 0
    known = known[(known["remaining"] != 0) & (np.sign(known["remaining"]) == np.sign(known["amount_minor"]))]

    base = known["expected_date"].copy()
    overdue = base < ts
    roll = np.where(known["source_type"] == "RECEIVABLE", fc["overdue_receivable_roll_days"], fc["overdue_payable_roll_days"])
    redated = pd.to_datetime(cal.roll_forward(ts + pd.to_timedelta(roll, unit="D")))
    base = pd.Series(np.where(overdue, redated, base), index=known.index)
    known["base_date"] = pd.to_datetime(base).astype("datetime64[ns]")
    window_end = ts + pd.Timedelta(days=int(fc["horizon_days"]) - 1)
    lines = known[known["base_date"] <= window_end].copy()

    reason = pd.Series(None, index=lines.index, dtype=object)
    links = ds["forecast_source_link"]
    settled_by_cutoff = set(act.loc[act["recorded_at"] < ts, "cf_id"])
    dup = set(duplicate_items(visible, links[links["cf_id"].isin(visible["cf_id"])], settled_by_cutoff)["cf_id"])
    mixed = set(currency_mismatch(lines, ds["bank_account"])["cf_id"])
    leak = set(basis_leakage(lines, act, ts)["cf_id"])
    reason[lines["cf_id"].isin(dup)] = "DUPLICATE_CASH_FLOW"
    reason[lines["cf_id"].isin(mixed)] = "MIXED_CURRENCY"
    reason[lines["cf_id"].isin(leak)] = "CUTOFF_LEAKAGE"
    lines["exclusion_reason"] = reason

    basis_ts = lines["basis_actual_id"].map(act.set_index("actual_id")["recorded_at"])
    lines["basis_record_id"] = lines["basis_actual_id"].where(lines["basis_actual_id"].notna(), lines["revision_id"])
    lines["basis_recorded_at"] = basis_ts.where(lines["basis_actual_id"].notna(), lines["revision_known_at"])
    lines["base_amount_minor"] = lines["remaining"].astype(np.int64)
    return lines[["cf_id", "revision_id", "account_id", "source_type", "source_id", "currency", "flow_category", "base_date",
                  "base_amount_minor", "settled_before_cutoff_minor", "basis_record_id", "basis_recorded_at",
                  "exclusion_reason", "known_at"]].reset_index(drop=True)


def opening_balances(ds: Dataset, cutoff: pd.Timestamp) -> pd.DataFrame:
    led, _ = split_ledger_by_currency(ds["ledger_entry"], ds["bank_account"])
    known = led[led["recorded_at"] < pd.Timestamp(cutoff)]
    agg = known.groupby("account_id")["amount_minor"].agg(["sum", "size"])
    acc = ds["bank_account"][["account_id", "currency", "entity_id", "opening_balance_minor"]].copy()
    acc["opening_balance_minor"] = acc["opening_balance_minor"].astype(np.int64) + acc["account_id"].map(agg["sum"]).fillna(0).astype(np.int64)
    acc["known_entry_count"] = acc["account_id"].map(agg["size"]).fillna(0).astype(np.int64)
    return acc


def sweep_lines(accounts: pd.DataFrame, opening: pd.DataFrame, lines: pd.DataFrame, cal, ts: pd.Timestamp,
                window_end: pd.Timestamp) -> pd.DataFrame:
    """FC-10: project the weekly cash-pool sweeps (LED-07) on forecast balances.

    On each sweep date in the window a pooled account's projected balance (known opening + included
    lines + earlier projected sweeps) is returned to its target; the difference moves to its parent.
    Uses only the opening balance known at cutoff and the run's own lines.
    """
    pool = accounts[accounts["sweep_parent_account_id"].notna()]
    dates = cal.weekly_sweep_dates(ts, window_end)
    if pool.empty or not dates:
        return pd.DataFrame()
    inc = lines[lines["line_status"] == "INCLUDED"]
    daily = inc.groupby(["account_id", "adjusted_date"])["adjusted_amount_minor"].sum()
    open_bal = opening.set_index("account_id")["opening_balance_minor"]
    rows = []
    for r in pool.sort_values("account_id").itertuples(index=False):
        if r.account_id in daily.index.get_level_values(0):
            flows = daily.xs(r.account_id)
        else:
            flows = pd.Series(dtype=np.int64, index=pd.DatetimeIndex([]))
        bal, prev = int(open_bal[r.account_id]), ts - pd.Timedelta(days=1)
        for d in dates:
            bal += int(flows[(flows.index > prev) & (flows.index <= d)].sum())
            x = bal - int(r.sweep_target_minor)
            prev = d
            if x == 0:
                continue
            bal -= x
            sid = f"SWEEP-{r.account_id}-{d:%Y%m%d}"
            for acct, amt in ((r.account_id, -x), (r.sweep_parent_account_id, x)):
                rows.append({"cf_id": None, "revision_id": None, "account_id": acct, "source_type": "SWEEP", "source_id": sid,
                             "currency": r.currency, "flow_category": "INTERNAL_TRANSFER", "base_date": d,
                             "base_amount_minor": amt, "adjusted_date": d, "adjusted_amount_minor": amt,
                             "settled_before_cutoff_minor": 0, "assumption_ids": None, "basis_record_id": r.account_id,
                             "basis_recorded_at": ts - pd.Timedelta(seconds=1), "line_status": "INCLUDED",
                             "exclusion_reason": None, "known_at": ts - pd.Timedelta(seconds=1)})
    return pd.DataFrame(rows)


def run_rate_lines(ds: Dataset, settings, cal, cutoff: pd.Timestamp) -> pd.DataFrame:
    """FC-12: recurring flows that have no forecast item yet, estimated from data recorded before cutoff."""
    rr = settings.forecast.get("run_rate") or {}
    if not rr.get("enabled"):
        return pd.DataFrame()
    ts = pd.Timestamp(cutoff)
    window_end = ts + pd.Timedelta(days=int(settings.forecast["horizon_days"]) - 1)
    led, _ = split_ledger_by_currency(ds["ledger_entry"], ds["bank_account"])
    known = led[led["recorded_at"] < ts]
    ccy = ds["bank_account"].set_index("account_id")["currency"]
    rows = []

    def line(acct, kind, sid, d, amt, basis_id, basis_ts):
        return {"cf_id": None, "revision_id": None, "account_id": acct, "source_type": "RUN_RATE", "source_id": sid,
                "currency": ccy[acct], "flow_category": kind, "base_date": d, "base_amount_minor": amt,
                "adjusted_date": d, "adjusted_amount_minor": amt, "settled_before_cutoff_minor": 0,
                "assumption_ids": None, "basis_record_id": basis_id, "basis_recorded_at": basis_ts,
                "line_status": "INCLUDED", "exclusion_reason": None, "known_at": basis_ts}

    if rr.get("payroll"):
        pay = known[known["entry_type"] == "PAYROLL"].sort_values(["account_id", "value_date", "ledger_entry_id"], kind="mergesort")
        last = pay.drop_duplicates("account_id", keep="last")
        actual_of = ds["actual_cash_flow"].set_index("ledger_entry_id")["actual_id"]
        visible = items_as_of(ds["cash_flow_forecast"], ds["cash_flow_forecast_revision"], ts)
        vp = visible[visible["flow_category"] == "PAYROLL"]
        booked = set(zip(vp["account_id"], vp["expected_date"].dt.to_period("M")))
        for m in pd.period_range(ts, window_end, freq="M"):
            pay_day = pd.Timestamp(cal.roll_backward([pd.Timestamp(m.year, m.month, int(rr["payroll_day"]))])[0])
            if not ts <= pay_day <= window_end:
                continue
            for r in last.itertuples(index=False):
                if (r.account_id, m) not in booked:
                    rows.append(line(r.account_id, "PAYROLL", f"RR-PAYROLL-{r.account_id}-{m.strftime('%Y%m')}", pay_day,
                                     int(r.amount_minor), actual_of.get(r.ledger_entry_id, r.ledger_entry_id), r.recorded_at))

    start = ts - pd.Timedelta(days=int(rr["lookback_days"]))
    hist = known[known["entry_type"].isin(rr["entry_types"]) & (known["value_date"] >= start)]
    n_hist = len(cal.business_days(start, ts - pd.Timedelta(days=1)))
    days = [pd.Timestamp(d) for d in cal.business_days(ts, window_end)]
    agg = hist.groupby("account_id").agg(net=("amount_minor", "sum"), last=("recorded_at", "max"))
    for acct, r in agg.sort_index().iterrows():
        per_day = money.div_half_even(int(r["net"]), n_hist) if n_hist else 0
        if per_day == 0:
            continue
        basis = f"RUNRATE-{acct}-{start:%Y%m%d}-{ts:%Y%m%d}"
        rows += [line(acct, "SUNDRY", f"RR-SUNDRY-{acct}-{d:%Y%m%d}", d, per_day, basis, r["last"]) for d in days]
    out = pd.DataFrame(rows)
    if len(out):
        for c in ["base_date", "adjusted_date"]:
            out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    return out


def execute_run(ds: Dataset, settings, cal, fx: FxLookup, spec: RunSpec, cand: pd.DataFrame,
                opening: pd.DataFrame, input_hash: str, recurring: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    rep = settings.reporting_currency
    mu = settings.minor_units
    H = int(settings.forecast["horizon_days"])
    ts = spec.cutoff
    window_end = ts + pd.Timedelta(days=H - 1)
    days = pd.date_range(ts, window_end, freq="D")

    lines = apply_scenario(cand, spec.code, settings, cal)
    lines["line_status"] = np.where(lines["exclusion_reason"].notna(), "EXCLUDED",
                                    np.where(lines["adjusted_date"] > window_end, "OUT_OF_HORIZON", "INCLUDED"))
    extra = one_off_line(settings, spec.code, ts, ds["bank_account"], fx, cal)
    if extra:
        lines = pd.concat([lines, pd.DataFrame([extra])], ignore_index=True)
    if recurring is not None and len(recurring):
        lines = pd.concat([lines, recurring], ignore_index=True)
    sweeps = sweep_lines(ds["bank_account"], opening, lines, cal, ts, window_end)
    if len(sweeps):
        lines = pd.concat([lines, sweeps], ignore_index=True)
    lines["forecast_run_id"] = spec.run_id
    lines = lines.sort_values(["account_id", "adjusted_date", "source_type", "source_id", "cf_id"],
                              kind="mergesort", na_position="first").reset_index(drop=True)
    lines["line_id"] = [f"{spec.run_id}-L{i:06d}" for i in range(1, len(lines) + 1)]

    acc = opening.copy()
    eff = [fx.effective(c, ts - pd.Timedelta(days=1)) for c in acc["currency"]]
    acc["rate_e8"] = [e[0] for e in eff]
    acc["fx_rate_date"] = pd.to_datetime([e[1] for e in eff])
    acc["fx_rate_status"] = [e[2] for e in eff]
    acc["fx_shock_bp"] = [fx_shock_bp(settings, spec.code, c) if c != rep else 0 for c in acc["currency"]]
    acc["applied_e8"] = [None if r is None else (r if c == rep else money.shock_rate(r, s))
                         for r, c, s in zip(acc["rate_e8"], acc["currency"], acc["fx_shock_bp"])]
    acc["fx_rate"] = [rate_text(r) if c != rep else None for r, c in zip(acc["rate_e8"], acc["currency"])]
    acc["fx_rate_applied"] = [rate_text(r) if c != rep else None for r, c in zip(acc["applied_e8"], acc["currency"])]
    acc["opening_balance_reporting_minor"] = pd.array(
        convert_to_reporting(acc["opening_balance_minor"], acc["currency"], acc["applied_e8"], mu, rep), dtype="Int64")
    acc["forecast_run_id"] = spec.run_id

    inc = lines[lines["line_status"] == "INCLUDED"]
    amt = inc["adjusted_amount_minor"].astype(np.int64)
    flows = pd.DataFrame({"account_id": inc["account_id"], "forecast_date": inc["adjusted_date"],
                          "inflows_minor": amt.clip(lower=0), "outflows_minor": (-amt).clip(lower=0)}
                         ).groupby(["account_id", "forecast_date"]).sum()
    grid = pd.MultiIndex.from_product([sorted(acc["account_id"]), days], names=["account_id", "forecast_date"])
    pos = flows.reindex(grid, fill_value=0).reset_index()
    pos[["inflows_minor", "outflows_minor"]] = pos[["inflows_minor", "outflows_minor"]].astype(np.int64)
    a = acc.set_index("account_id")
    net = pos["inflows_minor"] - pos["outflows_minor"]
    pos["closing_minor"] = pos["account_id"].map(a["opening_balance_minor"]).astype(np.int64) + net.groupby(pos["account_id"]).cumsum()
    pos["opening_minor"] = pos["closing_minor"] - net
    pos["currency"] = pos["account_id"].map(a["currency"])
    pos["horizon_day"] = (pos["forecast_date"] - ts).dt.days + 1
    rates = pos["account_id"].map(a["applied_e8"])
    pos["closing_reporting_minor"] = pd.array(convert_to_reporting(pos["closing_minor"], pos["currency"], rates, mu, rep), dtype="Int64")
    in_rep = convert_to_reporting(pos["inflows_minor"], pos["currency"], rates, mu, rep)
    out_rep = convert_to_reporting(pos["outflows_minor"], pos["currency"], rates, mu, rep)
    pos["forecast_run_id"] = spec.run_id

    tmp = pd.DataFrame({"entity_id": pos["account_id"].map(a["entity_id"]), "forecast_date": pos["forecast_date"],
                        "horizon_day": pos["horizon_day"],
                        "expected_inflows_reporting_minor": [x or 0 for x in in_rep],
                        "expected_outflows_reporting_minor": [x or 0 for x in out_rep],
                        "closing_reporting_minor": pos["closing_reporting_minor"].fillna(0).astype(np.int64)})
    ent = tmp.groupby(["entity_id", "forecast_date", "horizon_day"]).sum().reset_index().rename(columns={"entity_id": "scope_id"})
    ent["level"] = "ENTITY"
    ent["min_liquidity_reporting_minor"] = ent["scope_id"].map(ds["legal_entity"].set_index("entity_id")["min_liquidity_reporting_minor"])
    grp = ent.drop(columns=["scope_id", "level"]).groupby(["forecast_date", "horizon_day"]).sum().reset_index()
    grp["scope_id"], grp["level"] = "GROUP", "GROUP"
    cons = pd.concat([ent, grp], ignore_index=True)
    cons["below_threshold"] = cons["closing_reporting_minor"] < cons["min_liquidity_reporting_minor"]
    cons["forecast_run_id"] = spec.run_id
    for c in ["expected_inflows_reporting_minor", "expected_outflows_reporting_minor", "closing_reporting_minor",
              "min_liquidity_reporting_minor"]:
        cons[c] = cons[c].astype(np.int64)

    g = grp.sort_values("forecast_date")
    threshold = int(g["min_liquidity_reporting_minor"].iloc[0])
    below = g[g["closing_reporting_minor"] < threshold]
    imin = g["closing_reporting_minor"].idxmin()
    run = {
        "forecast_run_id": spec.run_id, "scenario_id": spec.scenario_id, "cutoff_date": ts, "cutoff_ts": ts,
        "generated_at": settings.generation_ts + pd.Timedelta(minutes=spec.order),
        "horizon_days": H, "horizon_end_date": window_end, "reporting_currency": rep, "input_hash": input_hash,
        "config_hash": settings.config_hash, "calculation_version": settings.calculation_version,
        "run_status": "EXECUTED", "supersedes_run_id": spec.supersedes, "rerun_reason": spec.reason,
        "included_line_count": int((lines["line_status"] == "INCLUDED").sum()),
        "excluded_line_count": int((lines["line_status"] == "EXCLUDED").sum()),
        "total_expected_inflows_reporting_minor": int(g["expected_inflows_reporting_minor"].sum()),
        "total_expected_outflows_reporting_minor": int(g["expected_outflows_reporting_minor"].sum()),
        "opening_group_cash_reporting_minor": int(acc["opening_balance_reporting_minor"].fillna(0).sum()),
        "min_group_cash_reporting_minor": int(g.loc[imin, "closing_reporting_minor"]),
        "min_group_cash_date": g.loc[imin, "forecast_date"],
        "group_threshold_reporting_minor": threshold,
        "first_group_breach_date": below["forecast_date"].iloc[0] if len(below) else pd.NaT,
        "group_days_below_threshold": int(len(below)),
        "negative_account_days": int((pos["closing_minor"] < 0).sum()),
    }
    return {"forecast_run": pd.DataFrame([run]), "forecast_run_account": acc, "forecast_run_line": lines,
            "forecast_position": pos, "forecast_consolidated_position": cons}


def execute_all(ds: Dataset, settings, cal, fx: FxLookup, input_hash: str) -> tuple[dict[str, pd.DataFrame], list[RunSpec]]:
    specs = plan_runs(settings)
    cache: dict[pd.Timestamp, tuple] = {}
    parts: dict[str, list] = {}
    for spec in specs:
        if spec.cutoff not in cache:
            cache[spec.cutoff] = (candidate_lines(ds, settings, cal, spec.cutoff), opening_balances(ds, spec.cutoff),
                                  run_rate_lines(ds, settings, cal, spec.cutoff))
        cand, opening, recurring = cache[spec.cutoff]
        for k, v in execute_run(ds, settings, cal, fx, spec, cand, opening, input_hash, recurring).items():
            parts.setdefault(k, []).append(v)
    return {k: pd.concat(v, ignore_index=True) for k, v in parts.items()}, specs
