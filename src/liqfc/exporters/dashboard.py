"""Static HTML dashboard built from a run's output folder.

One self-contained file (``reports/dashboard.html``): no server, no network, no external scripts.
Every number is read from the exported CSV/JSON outputs, so the page is a view, not a calculation.
The payload is serialised with sorted keys, so identical outputs give a byte-identical page.

Operational sections (positions, forecasts, runs, alerts, reconciliation, exceptions) read the
published dataset (``dataset/derived``, with planted anomalies). Accuracy and detection scores come
from ``reports/evaluation_summary.json`` (accuracy on the clean baseline, detection vs ground truth).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from liqfc.core import money
from liqfc.engine.fx_conversion import convert_to_reporting
from liqfc.exporters.csv_json import write_text

SCENARIOS = ["BASE", "DELAYED_RECEIPT", "STRESS"]
CAUSES = ["timing", "amount", "unforecast", "anomaly", "fx_movement", "residual"]
EFFECTS = ["delay", "haircut", "one_off", "fx"]
TOP_N = 10


def _csv(out: Path, name: str, part: str = "derived") -> pd.DataFrame:
    return pd.read_csv(out / "dataset" / part / "csv" / f"{name}.csv", keep_default_na=False, na_values=[""])


def _to_usd(amounts, currencies, rate_texts, settings) -> list[int]:
    """Local minor units to reporting minor units at the run's applied rates (0 where no rate)."""
    rates = [None if pd.isna(t) else money.parse_rate(t) for t in rate_texts]
    conv = convert_to_reporting(list(amounts), list(currencies), rates, settings.minor_units, settings.reporting_currency)
    return [0 if v is None else int(v) for v in conv]


def _scenario_effects(lines, run_acc, acct_entity, rid, base_rid, settings) -> dict:
    """Day-30 cash effect of each scenario assumption per entity, in reporting minor units.

    delay: customer receipts the scenario delays (and cuts); haircut: receipts only cut; one_off: the
    scenario-only payment; fx: change in the opening balances' reporting value from the FX shock.
    """
    a = run_acc[run_acc["forecast_run_id"] == rid].set_index("account_id")
    l = lines[lines["forecast_run_id"] == rid]
    ids = l["assumption_ids"].fillna("")
    kind = np.select([l["source_type"].eq("SCENARIO"), ids.str.contains("-01"), ids.str.contains("-03")],
                     ["one_off", "delay", "haircut"], "")
    l = l[kind != ""].assign(kind=kind[kind != ""])
    kept = np.where(l["line_status"] == "INCLUDED", l["adjusted_amount_minor"], 0)
    base = np.where(l["source_type"] == "SCENARIO", 0, l["base_amount_minor"])
    usd = _to_usd(kept - base, l["currency"], l["account_id"].map(a["fx_rate_applied"]), settings)
    eff = pd.DataFrame({"entity": l["account_id"].map(acct_entity).to_numpy(), "kind": l["kind"].to_numpy(), "usd": usd})
    out: dict = {}
    for (e, k), v in eff.groupby(["entity", "kind"])["usd"].sum().items():
        out.setdefault(e, {})[k] = int(v)
    opening = run_acc.assign(entity=run_acc["account_id"].map(acct_entity)).groupby(
        ["forecast_run_id", "entity"])["opening_balance_reporting_minor"].sum()
    for e, v in (opening.loc[rid] - opening.loc[base_rid]).items():
        if v:
            out.setdefault(e, {})["fx"] = int(v)
    return out


def _top_errors(va, run_acc, acct_entity, cf, actuals, rid, settings) -> list[dict]:
    """Largest contributors to the day-30 gap of one run, in reporting units.

    Rows are netted per source before ranking: a settlement's ledger entry counts against its forecast
    item, and a run-rate estimate against the same account-day's unforecast activity, so a forecast
    that was right doesn't show up as two large offsetting rows. The cause shown is the category with
    the largest net amount within the source.
    """
    v = va[(va["forecast_run_id"] == rid) & (va["source_type"] != "CASH_POOL_SWEEP")].copy()
    key = v["source_id"].copy()
    led = v["source_type"].eq("LEDGER_ENTRY")
    key[led] = v.loc[led, "source_id"].map(actuals.set_index("ledger_entry_id")["cf_id"]).fillna(v.loc[led, "source_id"])
    rr = v["source_type"].eq("RUN_RATE_ESTIMATE")
    key[rr] = v.loc[rr, "account_id"] + "|" + v.loc[rr, "effective_date"].astype(str)
    v["key"] = key
    a = run_acc[run_acc["forecast_run_id"] == rid].set_index("account_id")
    v["usd"] = _to_usd(v["amount_minor"], v["account_id"].map(a["currency"]), v["account_id"].map(a["fx_rate_applied"]), settings)
    tot = v.groupby("key").agg(usd=("usd", "sum"), first=("effective_date", "min"), account=("account_id", "first"))
    tot = tot[tot["usd"] != 0].assign(size=lambda t: t["usd"].abs()).reset_index()
    tot = tot.sort_values(["size", "key"], ascending=[False, True], kind="mergesort").head(TOP_N)
    by_cat = v.groupby(["key", "category"])["usd"].sum()
    entries = v[v["source_type"] == "LEDGER_GROUP"].groupby("key")["source_count"].sum()
    estimated = set(v.loc[rr, "key"])
    item = cf.set_index("cf_id")
    rows = []
    for r in tot.itertuples(index=False):
        cats = by_cat.loc[r.key]
        cause = cats.abs().sort_values(ascending=False, kind="mergesort").index[0]
        if r.key in item.index:
            it = item.loc[r.key]
            detail = f"forecast item: {it['flow_category'].replace('_', ' ').lower()} · {it['source_id']}"
            explanation = {"TIMING": "Settled on a different date than forecast, or after the horizon",
                           "AMOUNT": "Settled for a different amount than forecast, or will not settle",
                           "ANOMALY": "Excluded from the forecast as bad data; its settlement still moved cash"}.get(cause, cause)
        elif "|" in r.key:
            n = int(entries.get(r.key, 0))
            detail = f"{n} ledger entries" + (", net of the run-rate estimate" if r.key in estimated else "")
            explanation = "Cash moved with no forecast item" + (" beyond the run-rate estimate" if r.key in estimated else "")
        else:
            detail = None
            explanation = v.loc[v["key"] == r.key, "explanation"].iloc[0]
        rows.append({"category": cause, "source_id": r.key, "detail": detail, "account": r.account,
                     "entity": acct_entity.get(r.account), "date": r.first, "usd": int(r.usd), "explanation": explanation})
    return rows


def _int(v):
    return None if pd.isna(v) else int(v)


def _str(v):
    return None if pd.isna(v) else str(v)


def collect(out: Path, settings) -> dict:
    out = Path(out)
    ev = json.loads((out / "reports" / "evaluation_summary.json").read_text(encoding="utf-8"))
    val = json.loads((out / "reports" / "validation_summary.json").read_text(encoding="utf-8"))

    cons = _csv(out, "consolidated_cash_position")
    grp = cons[cons["level"] == "GROUP"].sort_values("position_date")
    group = {"dates": grp["position_date"].tolist(), "cash": [int(v) for v in grp["closing_balance_reporting_minor"]],
             "threshold": int(grp["min_liquidity_reporting_minor"].iloc[0])}
    cash_by_date = dict(zip(group["dates"], group["cash"]))

    runs_df = _csv(out, "forecast_run").sort_values("forecast_run_id")
    audit = _csv(out, "audit_event")
    held = audit[audit["action"] == "FORECAST_RUN_HELD"]
    hold_reasons = {oid: json.loads(d)["reasons"] for oid, d in zip(held["object_id"], held["details"])}
    alerts_df = _csv(out, "liquidity_alert").sort_values("alert_id")
    runs = []
    for r in runs_df.itertuples(index=False):
        runs.append({
            "id": r.forecast_run_id, "scenario": r.scenario_id.removeprefix("SCN-"), "cutoff": r.cutoff_date,
            "status": r.run_status, "supersedes": _str(r.supersedes_run_id), "rerun_reason": _str(r.rerun_reason),
            "opening": int(r.opening_group_cash_reporting_minor), "min_cash": int(r.min_group_cash_reporting_minor),
            "min_date": r.min_group_cash_date, "threshold": int(r.group_threshold_reporting_minor),
            "inflows": int(r.total_expected_inflows_reporting_minor),
            "outflows": int(r.total_expected_outflows_reporting_minor),
            "included": int(r.included_line_count), "excluded": int(r.excluded_line_count),
            "hold_reasons": hold_reasons.get(r.forecast_run_id, []),
            "alerts": int((alerts_df["forecast_run_id"] == r.forecast_run_id).sum()),
        })
    current = {(r["cutoff"], r["scenario"]): r["id"] for r in runs if r["status"] != "SUPERSEDED"}
    cutoffs = sorted({r["cutoff"] for r in runs})

    fcp_all = _csv(out, "forecast_consolidated_position").sort_values(["forecast_run_id", "scope_id", "forecast_date"])
    fcp = fcp_all[fcp_all["level"] == "GROUP"]
    fcp_ent = fcp_all[fcp_all["level"] == "ENTITY"]
    accounts = _csv(out, "bank_account", "input")
    acct_entity = dict(zip(accounts["account_id"], accounts["entity_id"]))
    run_acc = _csv(out, "forecast_run_account")
    lines = _csv(out, "forecast_run_line")
    va = _csv(out, "variance_attribution")
    cf = _csv(out, "cash_flow_forecast", "input")
    actuals = _csv(out, "actual_cash_flow", "input")
    comp = _csv(out, "forecast_comparison")
    last = comp[comp["horizon_day"] == comp.groupby("forecast_run_id")["horizon_day"].transform("max")]
    cols = ["variance"] + CAUSES
    var = last.groupby("forecast_run_id")[[f"{c}_reporting_minor" for c in cols]].sum()
    forecasts = {}
    for c in cutoffs:
        entry = {"runs": {}, "variance": {}, "entity": {}, "effects": {}}
        base_rid = current.get((c, "BASE"))
        for s in SCENARIOS:
            rid = current.get((c, s))
            if rid is None:
                continue
            f = fcp[fcp["forecast_run_id"] == rid]
            entry["dates"] = f["forecast_date"].tolist()
            entry["runs"][s] = {"id": rid, "cash": [int(v) for v in f["closing_reporting_minor"]]}
            if rid in var.index:
                entry["variance"][s] = {k: int(var.at[rid, f"{k}_reporting_minor"]) for k in cols}
            fe = fcp_ent[fcp_ent["forecast_run_id"] == rid]
            entry["entity"][s] = {e: [int(v) for v in g["closing_reporting_minor"]] for e, g in fe.groupby("scope_id")}
            if s != "BASE" and base_rid is not None:
                entry["effects"][s] = _scenario_effects(lines, run_acc, acct_entity, rid, base_rid, settings)
        entry["top_errors"] = _top_errors(va, run_acc, acct_entity, cf, actuals, base_rid, settings) if base_rid else []
        entry["actual"] = [cash_by_date.get(d) for d in entry.get("dates", [])]
        forecasts[c] = entry

    ents = _csv(out, "legal_entity", "input").sort_values("entity_id")
    entities = [{"id": e.entity_id, "name": e.entity_name, "currency": e.functional_currency,
                 "min": int(e.min_liquidity_reporting_minor)} for e in ents.itertuples(index=False)]
    ent_pos = cons[cons["level"] == "ENTITY"].sort_values(["scope_id", "position_date"])
    entity_actual = {e: [int(v) for v in g["closing_balance_reporting_minor"]] for e, g in ent_pos.groupby("scope_id")}

    alerts = [{"id": a.alert_id, "run": a.forecast_run_id, "level": a.level, "scope": a.scope_id,
               "threshold": int(a.threshold_reporting_minor), "min": int(a.min_projected_reporting_minor),
               "min_date": a.min_projected_date, "first_breach": _str(a.first_breach_date),
               "days_below": int(a.days_below_threshold), "severity": a.severity} for a in alerts_df.itertuples(index=False)]

    items = _csv(out, "bank_reconciliation_item")
    item_counts = items["match_status"].value_counts()
    pos = _csv(out, "daily_cash_position")
    day_counts = pos["reconciliation_status"].value_counts()
    brk = pos[pos["reconciliation_status"] == "BREAK"].sort_values(["position_date", "account_id"])
    breaks = [{"account": b.account_id, "date": b.position_date, "currency": b.currency,
               "unexplained": int(b.reconciliation_difference_minor - b.explained_difference_minor)}
              for b in brk.itertuples(index=False)]

    exc = _csv(out, "exception_log")
    exceptions = [{"rule": k[0], "issue": k[1], "severity": k[2], "decision": k[3], "n": int(n)}
                  for k, n in exc.groupby(["rule_id", "issue_type", "severity", "decision"]).size().items()]

    fa = ev["forecast_accuracy"]["baseline"]
    return {
        "meta": {"scale": settings.scale_name, "seed": settings.seed, "data_mode": settings.external["mode"],
                 "reporting_currency": settings.reporting_currency, "start": str(settings.start.date()),
                 "end": str(settings.end.date()), "config_hash": runs_df["config_hash"].iloc[0],
                 "minor_units": settings.minor_units},
        "group": group, "runs": runs, "cutoffs": cutoffs, "forecasts": forecasts, "alerts": alerts,
        "entities": entities, "entity_actual": entity_actual,
        "reconciliation": {
            "items": {k: int(v) for k, v in item_counts.items()},
            "days": {k: int(v) for k, v in day_counts.items()}, "breaks": breaks},
        "exceptions": exceptions,
        "accuracy": {"account_by_scenario": fa["account_level"]["by_scenario"],
                     "account_by_horizon": fa["account_level"]["by_horizon"],
                     "group_by_scenario": fa["group_level"], "overall": fa["account_level"]["overall"],
                     "rows": fa["comparison_rows"] - fa["rows_excluded_superseded_runs"]},
        "detection": {"by_type": ev["detection"]["injected"],
                      "clean_false_positives": ev["detection"]["baseline_false_positives"]},
        "validation": [{"stage": k, "passed": v["passed"], "failed": v["failed"],
                        "failed_checks": [{"id": c["check_id"], "name": c["name"], "failures": c["failures"]}
                                          for c in v["failed_checks"]]} for k, v in val.items()],
    }


def render(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    title = f"Liquidity Dashboard ({data['meta']['scale']})"
    return TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload)


def build_dashboard(out: Path, settings) -> Path:
    path = Path(out) / "reports" / "dashboard.html"
    write_text(path, render(collect(out, settings)))
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-ink: #0b0b0b;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --good-text: #006300; --wash: rgba(42,120,214,0.08);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-ink: #f0efec;
    --good-text: #0ca30c; --wash: rgba(57,135,229,0.14);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-ink: #f0efec;
  --good-text: #0ca30c; --wash: rgba(57,135,229,0.14);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1240px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; font-weight: 650; }
h2 { font-size: 16px; margin: 0 0 2px; font-weight: 600; }
h3 { font-size: 13px; margin: 0 0 6px; font-weight: 600; color: var(--text-secondary); }
.sub, .note { color: var(--text-secondary); margin: 0; }
.note { font-size: 12.5px; }
.divider { margin: 36px 0 12px; padding-top: 16px; border-top: 1px solid var(--grid); }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 20px 0 16px; }
.filters .lab { color: var(--text-secondary); margin-right: 4px; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--surface-1); }
.seg button { font: inherit; border: 0; background: transparent; color: var(--text-primary); padding: 6px 12px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--border); }
.seg button:hover { background: var(--wash); }
.seg button[aria-pressed="true"] { background: var(--text-primary); color: var(--surface-1); font-weight: 600; }
.tiles { display: grid; grid-template-columns: 1.4fr repeat(4, 1fr); gap: 12px; }
.tile, .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 16px; min-width: 0; }
.tile .label { color: var(--text-secondary); font-size: 12.5px; }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
.tile.hero .value { font-size: 48px; line-height: 1.1; }
.tile .delta { color: var(--text-secondary); font-size: 12.5px; margin-top: 2px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.card { margin-top: 12px; }
.grid2 > .card, .grid3 > .card { margin-top: 0; }
.card header { margin-bottom: 10px; }
.chart { width: 100%; position: relative; }
.chart svg { display: block; overflow: visible; }
.chart svg:focus { outline: 2px solid var(--series-1); outline-offset: 2px; border-radius: 4px; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.rowlab { fill: var(--text-secondary); font-size: 12px; }
.val { fill: var(--text-primary); font-size: 12px; font-variant-numeric: tabular-nums; }
.reflab { fill: var(--text-secondary); font-size: 11px; }
.hit { fill: transparent; cursor: default; }
.hit:hover + .bar, .hit:focus + .bar { opacity: 0.8; }
.hit:focus { outline: none; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 0 0 8px; color: var(--text-secondary); font-size: 12.5px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.key { display: inline-block; width: 14px; height: 2px; border-radius: 1px; }
.key.ref { height: 1.5px; background: var(--text-secondary); }
#tip { position: fixed; z-index: 10; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); display: none; min-width: 150px; }
#tip .hd { color: var(--text-secondary); margin-bottom: 4px; }
#tip .row { display: flex; align-items: center; gap: 8px; }
#tip .row b { font-variant-numeric: tabular-nums; }
#tip .row span:last-child { color: var(--text-secondary); }
details { margin-top: 8px; }
summary { cursor: pointer; color: var(--text-secondary); font-size: 12.5px; }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 6px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.nw { white-space: nowrap; }
#t-runs table { min-width: 640px; }
#t-top table { min-width: 760px; }
select { font: inherit; color: var(--text-primary); background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 8px; max-width: 100%; }
.grid2.inner { margin-top: 0; }
td.neg { color: var(--critical); }
#t-alerts table, #t-accuracy table, #t-detection table { min-width: 460px; }
tr.sel td { background: var(--wash); }
.badge { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.empty { color: var(--text-secondary); padding: 8px 0; }
.reasons { margin: 4px 0 0; padding-left: 16px; color: var(--text-secondary); }
footer { margin-top: 32px; color: var(--text-muted); font-size: 12px; }
@media (max-width: 980px) { .tiles { grid-template-columns: 1fr 1fr; } .tile.hero { grid-column: 1 / -1; } .grid3 { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .grid2 { grid-template-columns: 1fr; } }
@media (max-width: 420px) { .tiles { grid-template-columns: 1fr; } .tile.hero .value { font-size: 40px; } }
</style>
</head>
<body>
<main>
  <h1>Liquidity dashboard</h1>
  <p class="sub" id="meta"></p>
  <p class="note">Positions, forecasts, runs, alerts, reconciliation and exceptions come from the published dataset, which
    includes the planted anomalies. Accuracy is measured on the clean baseline; detection is scored against the separate ground truth.</p>

  <div class="filters" role="group" aria-label="Forecast cutoff">
    <span class="lab">Forecast cutoff</span>
    <div class="seg" id="cutoffs"></div>
    <label class="lab" for="entity" style="margin-left:12px">Entity</label>
    <select id="entity"></select>
  </div>

  <section class="tiles" id="tiles" aria-live="polite"></section>

  <div class="grid2">
    <section class="card">
      <header><h2>Group cash, actual</h2><p class="note" id="hist-note"></p></header>
      <div class="chart" id="c-history"></div>
      <details><summary>Show table</summary><div class="tablewrap" id="t-history"></div></details>
    </section>
    <section class="card">
      <header><h2 id="fc-title">Forecast vs actual</h2><p class="note" id="fc-note"></p></header>
      <div class="legend" id="l-forecast"></div>
      <div class="chart" id="c-forecast"></div>
      <details><summary>Show table</summary><div class="tablewrap" id="t-forecast"></div></details>
    </section>
  </div>

  <section class="card">
    <header><h2>What explains the gap on the last forecast day</h2>
      <p class="note">Actual minus forecast group cash on day 30, split into causes (USD). Positive means more cash than forecast. Unforecast is net of the run-rate estimate for payroll not yet booked and small flows. The causes add up to the total.</p></header>
    <div class="grid3" id="c-variance"></div>
    <details><summary>Show table</summary><div class="tablewrap" id="t-variance"></div></details>
  </section>

  <section class="card">
    <header><h2 id="ent-title">Entity drill-down</h2><p class="note" id="ent-note"></p></header>
    <div class="grid2 inner">
      <div>
        <h3>Projected cash vs actual</h3>
        <div class="legend" id="l-entity"></div>
        <div class="chart" id="c-entity"></div>
        <details><summary>Show table</summary><div class="tablewrap" id="t-entity"></div></details>
      </div>
      <div>
        <h3>What each scenario changes for this entity (cash on day 30)</h3>
        <div class="tablewrap" id="t-effects"></div>
        <p class="note" style="margin-top:8px">Negative values reduce cash. Delayed receipts that still arrive inside the 30 days
          only change the path, not day 30. FX shock is the change in the USD value of the opening balances.</p>
      </div>
    </div>
  </section>

  <section class="card">
    <header><h2>Largest contributors to the day-30 gap, base forecast</h2>
      <p class="note">Each source's net contribution to actual minus forecast cash on the last forecast day, in USD (cash-pool sweeps excluded:
        they net to zero within a pool). Positive means more cash than forecast.</p></header>
    <div class="tablewrap" id="t-top"></div>
  </section>

  <div>
    <section class="card">
      <header><h2>Forecast runs at this cutoff</h2><p class="note">Approval policy RUN-01: a run is held only when its own inputs are affected.</p></header>
      <div class="tablewrap" id="t-runs"></div>
    </section>
    <section class="card">
      <header><h2>Liquidity alerts at this cutoff</h2><p class="note">Projected breaches of each entity's minimum-cash policy.</p></header>
      <div class="tablewrap" id="t-alerts"></div>
    </section>
  </div>

  <div class="divider"><h2>Whole period</h2><p class="note" id="period-note"></p></div>

  <div class="grid2">
    <section class="card">
      <header><h2>Forecast error by horizon</h2><p class="note" id="acc-note"></p></header>
      <div class="chart" id="c-horizon"></div>
      <details open><summary>Accuracy by scenario</summary><div class="tablewrap" id="t-accuracy"></div></details>
    </section>
    <section class="card">
      <header><h2>Bank reconciliation</h2><p class="note">Every ledger entry and bank line appears in exactly one reconciliation item.</p></header>
      <div class="chart" id="c-recon"></div>
      <div class="tablewrap" id="t-recon-days"></div>
      <details><summary>Unexplained breaks</summary><div class="tablewrap" id="t-breaks"></div></details>
    </section>
  </div>

  <div class="grid2">
    <section class="card">
      <header><h2>Planted problems found</h2><p class="note" id="det-note"></p></header>
      <div class="tablewrap" id="t-detection"></div>
    </section>
    <section class="card">
      <header><h2>Exception log</h2><p class="note">Findings by rule, with the documented decision.</p></header>
      <div class="chart" id="c-exceptions"></div>
      <details><summary>Show table</summary><div class="tablewrap" id="t-exceptions"></div></details>
    </section>
  </div>

  <section class="card">
    <header><h2>Validation</h2><p class="note">Failures on the injected dataset are expected: each is caused by a planted anomaly.</p></header>
    <div class="tablewrap" id="t-validation"></div>
  </section>

  <footer id="foot"></footer>
</main>
<div id="tip" role="status"></div>
<script type="application/json" id="dash-data">__DATA__</script>
<script>
(function () {
"use strict";
var D = JSON.parse(document.getElementById("dash-data").textContent);
var NS = "http://www.w3.org/2000/svg";
var SC = ["BASE", "DELAYED_RECEIPT", "STRESS"];
var SC_LABEL = {BASE: "Base", DELAYED_RECEIPT: "Delayed receipts", STRESS: "Stress"};
var SC_COLOR = {BASE: "var(--series-1)", DELAYED_RECEIPT: "var(--series-2)", STRESS: "var(--series-3)"};
var CAUSE_LABEL = {timing: "Timing", amount: "Amount", unforecast: "Unforecast", anomaly: "Bad data excluded",
  fx_movement: "FX movement", residual: "Rounding"};
var STATUS = {
  APPROVED: ["var(--good)", "Approved"], EXECUTED: ["var(--warning)", "Held for review"],
  SUPERSEDED: ["var(--text-muted)", "Superseded"], CRITICAL: ["var(--critical)", "Critical"],
  WARNING: ["var(--warning)", "Warning"]
};
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
var state = {cutoff: D.cutoffs[D.cutoffs.length - 1], entity: null};
var EFFECT_LABEL = {delay: "Customer receipts delayed", haircut: "Collection haircut on receipts",
  one_off: "Unplanned one-off payment", fx: "FX shock on opening balances"};
var EFFECT_ORDER = ["delay", "haircut", "one_off", "fx"];

function $(id) { return document.getElementById(id); }
function svgEl(tag, attrs, parent) {
  var e = document.createElementNS(NS, tag);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function h(tag, attrs, text, parent) {
  var e = document.createElement(tag);
  if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}
function usd(cents, dp) {
  if (cents == null) return "–";
  var v = cents / 100, a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e9) return s + "$" + (a / 1e9).toFixed(dp == null ? 2 : dp) + "B";
  if (a >= 1e6) return s + "$" + (a / 1e6).toFixed(dp == null ? 1 : dp) + "M";
  if (a >= 1e3) return s + "$" + (a / 1e3).toFixed(dp == null ? 1 : dp) + "K";
  return s + "$" + a.toFixed(0);
}
function minor(amount, ccy) {
  var u = D.meta.minor_units[ccy] || 0, v = amount / Math.pow(10, u);
  return (v < 0 ? "−" : "") + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: u, maximumFractionDigits: u}) + " " + ccy;
}
function count(n) { return n.toLocaleString("en-US"); }
function pct(x) { return x.toFixed(1) + "%"; }
function day(d) { var p = d.split("-"); return MONTHS[+p[1] - 1] + " " + (+p[2]); }
function niceTicks(lo, hi, n) {
  if (lo === hi) { lo -= 1; hi += 1; }
  var step = Math.pow(10, Math.floor(Math.log10((hi - lo) / n))), err = (hi - lo) / n / step;
  step *= err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
  var t = [], v = Math.floor(lo / step) * step;
  for (; v <= hi + step * 0.5; v += step) t.push(v);
  if (t[t.length - 1] < hi) t.push(t[t.length - 1] + step);
  return t;
}
function badge(code, parent) {
  var s = STATUS[code] || ["var(--text-muted)", code];
  var b = h("span", {"class": "badge"}, null, parent);
  var d = h("span", {"class": "dot"}, null, b); d.style.background = s[0];
  h("span", null, s[1], b);
  return b;
}

var tip = $("tip");
function showTip(evt, head, rows) {
  tip.textContent = "";
  h("div", {"class": "hd"}, head, tip);
  rows.forEach(function (r) {
    var row = h("div", {"class": "row"}, null, tip);
    if (r.color) { var k = h("span", {"class": "key"}, null, row); k.style.background = r.color; }
    h("b", null, r.value, row);
    if (r.name) h("span", null, r.name, row);
  });
  tip.style.display = "block";
  var x, y;
  if (evt && evt.clientX != null && evt.type.indexOf("pointer") === 0) { x = evt.clientX; y = evt.clientY; }
  else { var bb = evt.target.getBoundingClientRect(); x = bb.left + bb.width / 2; y = bb.top; }
  var w = tip.offsetWidth, ht = tip.offsetHeight;
  var left = x + 14, top = y - ht - 10;
  if (left + w > window.innerWidth - 8) left = x - w - 14;
  if (left < 8) left = 8;
  if (top < 8) top = y + 16;
  tip.style.left = left + "px"; tip.style.top = top + "px";
}
function hideTip() { tip.style.display = "none"; }

function legend(host, items) {
  host.textContent = "";
  items.forEach(function (it) {
    var s = h("span", null, null, host);
    var k = h("span", {"class": "key" + (it.ref ? " ref" : "")}, null, s);
    if (!it.ref) k.style.background = it.color;
    h("span", null, it.name, s);
  });
}

function table(host, cols, rows, opts) {
  host.textContent = "";
  if (!rows.length) { h("div", {"class": "empty"}, (opts && opts.empty) || "Nothing to show.", host); return; }
  var t = h("table", null, null, host), tr = h("tr", null, null, h("thead", null, null, t));
  cols.forEach(function (c) { h("th", c.num ? {"class": "num"} : null, c.label, tr); });
  var tb = h("tbody", null, null, t);
  rows.forEach(function (r) {
    var row = h("tr", opts && opts.sel && opts.sel(r) ? {"class": "sel"} : null, null, tb);
    cols.forEach(function (c) {
      var td = h("td", c.num ? {"class": "num"} : c.nowrap ? {"class": "nw"} : null, null, row);
      var v = c.get(r);
      if (v instanceof Node) td.appendChild(v); else td.textContent = v == null ? "–" : v;
    });
  });
}

// Line chart: crosshair snaps to the nearest date; the tooltip lists every series.
function lineChart(host, cfg) {
  host.textContent = "";
  var W = Math.max(host.clientWidth, 280), H = cfg.height || 260, m = {l: 58, r: 12, t: 12, b: 26};
  var vals = [];
  cfg.series.forEach(function (s) { s.values.forEach(function (v) { if (v != null) vals.push(v); }); });
  (cfg.refs || []).forEach(function (r) { vals.push(r.value); });
  var ticks = niceTicks(Math.min.apply(null, vals), Math.max.apply(null, vals), 5);
  var lo = ticks[0], hi = ticks[ticks.length - 1], n = cfg.dates.length;
  function x(i) { return m.l + (n < 2 ? 0 : i * (W - m.l - m.r) / (n - 1)); }
  function y(v) { return m.t + (hi - v) * (H - m.t - m.b) / (hi - lo); }
  var svg = svgEl("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
    "aria-label": cfg.label + ". Use the left and right arrow keys to read values.", tabindex: 0}, host);
  ticks.forEach(function (t) {
    svgEl("line", {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), "class": t === lo ? "axis" : "grid"}, svg);
    svgEl("text", {x: m.l - 8, y: y(t) + 4, "class": "tick", "text-anchor": "end"}, svg).textContent = usd(t, 0);
  });
  var lastX = -1e9;
  cfg.xticks.forEach(function (i) {
    if (x(i) - lastX < 48) return;
    lastX = x(i);
    svgEl("text", {x: x(i), y: H - 6, "class": "tick", "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle"}, svg)
      .textContent = day(cfg.dates[i]);
  });
  (cfg.marks || []).forEach(function (mk) {
    svgEl("line", {x1: x(mk.index), x2: x(mk.index), y1: m.t, y2: H - m.b, style: "stroke:var(--text-secondary)", "stroke-width": 1}, svg);
    svgEl("text", {x: x(mk.index) + 4, y: m.t + 10, "class": "reflab"}, svg).textContent = mk.name;
  });
  (cfg.refs || []).forEach(function (r) {
    svgEl("line", {x1: m.l, x2: W - m.r, y1: y(r.value), y2: y(r.value), style: "stroke:var(--text-secondary)", "stroke-width": 1.5}, svg);
    svgEl("text", {x: m.l + 4, y: y(r.value) - 5, "class": "reflab"}, svg).textContent = r.name + " " + usd(r.value);
  });
  cfg.series.forEach(function (s) {
    var d = "", pen = false;
    s.values.forEach(function (v, i) {
      if (v == null) { pen = false; return; }
      d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); pen = true;
    });
    svgEl("path", {d: d, fill: "none", style: "stroke:" + s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
  });
  var cross = svgEl("line", {y1: m.t, y2: H - m.b, style: "stroke:var(--axis)", "stroke-width": 1, visibility: "hidden"}, svg);
  var dots = cfg.series.map(function (s) {
    return svgEl("circle", {r: 4, style: "fill:" + s.color + ";stroke:var(--surface-1)", "stroke-width": 2, visibility: "hidden"}, svg);
  });
  var cur = -1;
  function show(i, evt) {
    cur = i;
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    var rows = [];
    cfg.series.forEach(function (s, k) {
      var v = s.values[i];
      if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
      dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(v)); dots[k].setAttribute("visibility", "visible");
      rows.push({color: s.color, value: usd(v), name: s.name});
    });
    showTip(evt, day(cfg.dates[i]) + " 2026", rows);
  }
  function hide() { cross.setAttribute("visibility", "hidden"); dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); }); hideTip(); }
  var ov = svgEl("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"}, svg);
  ov.addEventListener("pointermove", function (e) {
    var r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * W / r.width;
    show(Math.max(0, Math.min(n - 1, Math.round((px - m.l) / ((W - m.l - m.r) / Math.max(n - 1, 1))))), e);
  });
  ov.addEventListener("pointerleave", hide);
  svg.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    show(Math.max(0, Math.min(n - 1, (cur < 0 ? n - 1 : cur + (e.key === "ArrowLeft" ? -1 : 1)))), {target: svg, type: "key"});
  });
  svg.addEventListener("blur", hide);
}

// Horizontal bar chart from one baseline (zero); value at each bar's tip; each row is a focusable hit target.
function barChart(host, cfg) {
  host.textContent = "";
  var W = Math.max(host.clientWidth, 260), rowH = 30, barH = 16, m = {l: cfg.labelWidth || 130, r: 8, t: 4, b: 4};
  var H = m.t + m.b + rowH * cfg.rows.length;
  var vals = cfg.rows.map(function (r) { return r.value; });
  var lo = Math.min(0, Math.min.apply(null, vals)), hi = Math.max(0, Math.max.apply(null, vals));
  var room = 64, left = lo < 0 ? room : 0, right = hi > 0 || lo === 0 ? room : 0;
  var k = Math.max(W - m.l - m.r - left - right, 20) / ((hi - lo) || 1);
  function x(v) { return m.l + left + (v - lo) * k; }
  var svg = svgEl("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": cfg.label}, host);
  svgEl("line", {x1: x(0), x2: x(0), y1: m.t, y2: H - m.b, "class": "axis"}, svg);
  cfg.rows.forEach(function (r, i) {
    var y0 = m.t + i * rowH, by = y0 + (rowH - barH) / 2;
    svgEl("text", {x: m.l - 10, y: y0 + rowH / 2 + 4, "class": "rowlab", "text-anchor": "end"}, svg).textContent = r.label;
    var hit = svgEl("rect", {x: 0, y: y0, width: W, height: rowH, "class": "hit", tabindex: 0,
      "aria-label": r.label + ": " + cfg.fmt(r.value)}, svg);
    var x0 = x(0), x1 = x(r.value), w = Math.abs(x1 - x0), rad = Math.min(4, w / 2), d;
    if (w < 0.5) d = "M" + x0 + " " + by + "h0.5v" + barH + "h-0.5Z";
    else if (r.value >= 0) d = "M" + x0 + " " + by + "H" + (x1 - rad) + "Q" + x1 + " " + by + " " + x1 + " " + (by + rad) +
      "V" + (by + barH - rad) + "Q" + x1 + " " + (by + barH) + " " + (x1 - rad) + " " + (by + barH) + "H" + x0 + "Z";
    else d = "M" + x0 + " " + by + "H" + (x1 + rad) + "Q" + x1 + " " + by + " " + x1 + " " + (by + rad) +
      "V" + (by + barH - rad) + "Q" + x1 + " " + (by + barH) + " " + (x1 + rad) + " " + (by + barH) + "H" + x0 + "Z";
    svgEl("path", {d: d, "class": "bar", style: "fill:" + cfg.color, "pointer-events": "none"}, svg);
    var neg = r.value < 0;
    svgEl("text", {x: neg ? x1 - 6 : x1 + 6, y: y0 + rowH / 2 + 4, "class": "val", "text-anchor": neg ? "end" : "start",
      "pointer-events": "none"}, svg).textContent = cfg.fmt(r.value);
    function tipOn(e) { showTip(e, r.label, [{color: cfg.color, value: cfg.fmt(r.value), name: r.note || ""}]); }
    hit.addEventListener("pointermove", tipOn);
    hit.addEventListener("focus", tipOn);
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("blur", hideTip);
  });
}

function runsAt(c) { return D.runs.filter(function (r) { return r.cutoff === c; }); }
function currentRun(c, s) {
  return D.runs.filter(function (r) { return r.cutoff === c && r.scenario === s && r.status !== "SUPERSEDED"; })[0];
}

function renderMeta() {
  var m = D.meta;
  $("meta").textContent = (m.scale === "full" ? "Full" : "Starter") + " scale · seed " + m.seed + " · " + m.data_mode +
    " data · reporting currency " + m.reporting_currency + " · " + m.start + " to " + m.end;
  $("period-note").textContent = "Figures for the full period, " + m.start + " to " + m.end + "; not affected by the cutoff filter.";
  $("foot").textContent = "Generated from this run's output files (config " + m.config_hash.slice(0, 12) +
    "). Every value here is read from the exported CSV and JSON outputs listed in manifest.json.";
  var seg = $("cutoffs");
  D.cutoffs.forEach(function (c) {
    var b = h("button", {type: "button", "aria-pressed": String(c === state.cutoff)}, day(c), seg);
    b.addEventListener("click", function () {
      state.cutoff = c;
      Array.prototype.forEach.call(seg.children, function (x) { x.setAttribute("aria-pressed", String(x === b)); });
      renderScoped();
    });
  });
}

function tile(host, label, value, delta, hero) {
  var t = h("div", {"class": "tile" + (hero ? " hero" : "")}, null, host);
  h("div", {"class": "label"}, label, t);
  h("div", {"class": "value"}, value, t);
  if (delta) h("div", {"class": "delta"}, delta, t);
}

function renderTiles() {
  var host = $("tiles"), c = state.cutoff;
  host.textContent = "";
  var base = currentRun(c, "BASE"), stress = currentRun(c, "STRESS"), runs = runsAt(c).filter(function (r) { return r.status !== "SUPERSEDED"; });
  var alerts = D.alerts.filter(function (a) { return runs.some(function (r) { return r.id === a.run; }); });
  var held = runs.filter(function (r) { return r.status === "EXECUTED"; }).length;
  tile(host, "Group cash at the " + day(c) + " cutoff", usd(base.opening, 2), "Opening position of the base forecast", true);
  tile(host, "Lowest projected group cash, base", usd(base.min_cash), (base.min_cash / base.threshold).toFixed(1) + "× the policy minimum, on " + day(base.min_date));
  tile(host, "Stress impact on lowest cash", usd(stress.min_cash - base.min_cash), "Stress vs base, lowest point in 30 days");
  tile(host, "Liquidity alerts", count(alerts.length), alerts.length ? alerts.map(function (a) { return a.severity.toLowerCase() + ": " + a.scope; }).join(", ") : "No entity breaches its minimum");
  tile(host, "Runs held for review", held + " of " + runs.length, held ? "See the reasons in the runs table" : "All runs approved");
}

function renderHistory() {
  var g = D.group, idx = g.dates.indexOf(state.cutoff), ticks = [];
  g.dates.forEach(function (d, i) { if (d.slice(8) === "01") ticks.push(i); });
  $("hist-note").textContent = "Daily closing cash of all " + "accounts in USD, " + day(g.dates[0]) + " to " + day(g.dates[g.dates.length - 1]) + ". The vertical line marks the selected cutoff.";
  lineChart($("c-history"), {dates: g.dates, xticks: ticks, label: "Group cash, actual, daily",
    series: [{name: "Group cash", color: "var(--series-1)", values: g.cash}],
    refs: [{name: "Policy minimum", value: g.threshold}],
    marks: idx >= 0 ? [{index: idx, name: "Cutoff"}] : []});
  table($("t-history"), [{label: "Month end", get: function (r) { return r[0]; }}, {label: "Group cash", num: 1, get: function (r) { return usd(r[1], 2); }}],
    g.dates.map(function (d, i) { return [d, g.cash[i]]; }).filter(function (r, i, a) { return i === a.length - 1 || a[i + 1][0].slice(8) === "01"; }));
}

function renderForecast() {
  var f = D.forecasts[state.cutoff];
  $("fc-title").textContent = "Forecast vs actual from the " + day(state.cutoff) + " cutoff";
  var series = [{name: "Actual", color: "var(--series-ink)", values: f.actual}];
  SC.forEach(function (s) { if (f.runs[s]) series.push({name: SC_LABEL[s], color: SC_COLOR[s], values: f.runs[s].cash}); });
  legend($("l-forecast"), series.map(function (s) { return {name: s.name, color: s.color}; }));
  $("fc-note").textContent = "Projected group cash for each scenario against what actually happened. The policy minimum (" +
    usd(D.group.threshold) + ") is far below this range, so the axis starts above it.";
  var ticks = [];
  for (var i = 0; i < f.dates.length; i += 7) ticks.push(i);
  if (ticks[ticks.length - 1] !== f.dates.length - 1) ticks.push(f.dates.length - 1);
  if (ticks.length > 1 && ticks[ticks.length - 1] - ticks[ticks.length - 2] < 4) ticks.splice(ticks.length - 2, 1);
  lineChart($("c-forecast"), {dates: f.dates, xticks: ticks, label: "Projected and actual group cash for 30 days after the cutoff",
    series: series});
  table($("t-forecast"), [{label: "Date", get: function (r) { return r; }}].concat(series.map(function (s) {
    return {label: s.name, num: 1, get: function (d) { return usd(s.values[f.dates.indexOf(d)], 2); }};
  })), f.dates);
}

function renderVariance() {
  var f = D.forecasts[state.cutoff], host = $("c-variance");
  host.textContent = "";
  var rows = [];
  SC.forEach(function (s) {
    var v = f.variance[s];
    if (!v) return;
    var box = h("div", null, null, host);
    h("h3", null, SC_LABEL[s] + " · total " + usd(v.variance), box);
    var chart = h("div", {"class": "chart"}, null, box);
    var causes = ["timing", "amount", "unforecast", "anomaly", "fx_movement"];
    barChart(chart, {label: "Causes of the day-30 gap, " + SC_LABEL[s], color: SC_COLOR[s], labelWidth: 118,
      fmt: function (x) { return usd(x); },
      rows: causes.map(function (k) { return {label: CAUSE_LABEL[k], value: v[k]}; })});
    rows.push([s, v]);
  });
  table($("t-variance"), [{label: "Scenario", get: function (r) { return SC_LABEL[r[0]]; }}].concat(
    ["variance", "timing", "amount", "unforecast", "anomaly", "fx_movement", "residual"].map(function (k) {
      return {label: k === "variance" ? "Total" : CAUSE_LABEL[k], num: 1, get: function (r) { return usd(r[1][k], 2); }};
    })), rows);
}

function renderRuns() {
  var rows = runsAt(state.cutoff);
  table($("t-runs"), [
    {label: "Run", nowrap: 1, get: function (r) { return r.id; }},
    {label: "Scenario", get: function (r) { return SC_LABEL[r.scenario]; }},
    {label: "Status", get: function (r) {
      var wrap = document.createElement("div");
      badge(r.status, wrap);
      var notes = r.status === "SUPERSEDED" ? [] : r.hold_reasons.slice(0, 3);
      if (r.hold_reasons.length > 3 && r.status !== "SUPERSEDED") notes.push("and " + (r.hold_reasons.length - 3) + " more");
      if (r.supersedes) notes.push("Re-run of " + r.supersedes + (r.rerun_reason ? ": " + r.rerun_reason : ""));
      if (notes.length) { var ul = h("ul", {"class": "reasons"}, null, wrap); notes.forEach(function (n) { h("li", null, n, ul); }); }
      return wrap;
    }},
    {label: "Lowest group cash", num: 1, get: function (r) { return usd(r.min_cash); }},
    {label: "Lines used / excluded", num: 1, get: function (r) { return count(r.included) + " / " + count(r.excluded); }}
  ], rows);
}

function renderAlerts() {
  var ids = runsAt(state.cutoff).map(function (r) { return r.id; });
  table($("t-alerts"), [
    {label: "Severity", get: function (a) { return badge(a.severity, document.createElement("div")); }},
    {label: "Entity", get: function (a) { return a.scope; }},
    {label: "Run", nowrap: 1, get: function (a) { return a.run; }},
    {label: "Lowest projected", num: 1, get: function (a) { return usd(a.min); }},
    {label: "Minimum", num: 1, get: function (a) { return usd(a.threshold); }},
    {label: "First breach", nowrap: 1, get: function (a) { return a.first_breach; }},
    {label: "Days below", num: 1, get: function (a) { return a.days_below; }}
  ], D.alerts.filter(function (a) { return ids.indexOf(a.run) >= 0; }), {empty: "No entity breaches its minimum cash at this cutoff."});
}

function renderPeriod() {
  var A = D.accuracy, order = ["D01-07", "D08-14", "D15-21", "D22-30"];
  var hz = order.filter(function (k) { return A.account_by_horizon[k]; });
  $("acc-note").textContent = "Average error (MAPE) per account and day on the clean data, all scenarios, " + count(A.rows) + " forecast-vs-actual rows.";
  barChart($("c-horizon"), {label: "Forecast error by horizon", color: "var(--series-1)", labelWidth: 96,
    fmt: pct, rows: hz.map(function (k) { return {label: "Days " + k.slice(1).replace("-", "–").replace(/^0/, ""), value: A.account_by_horizon[k].mape_pct}; })});
  table($("t-accuracy"), [
    {label: "Scenario", get: function (s) { return SC_LABEL[s]; }},
    {label: "Error per account", num: 1, get: function (s) { return pct(A.account_by_scenario[s].mape_pct); }},
    {label: "Error, group", num: 1, get: function (s) { return pct(A.group_by_scenario[s].mape_pct); }},
    {label: "Avg miss per account", num: 1, get: function (s) { return usd(A.account_by_scenario[s].mae * 100); }},
    {label: "Bias", num: 1, get: function (s) { return usd(A.account_by_scenario[s].bias * 100); }}
  ], SC.filter(function (s) { return A.account_by_scenario[s]; }));

  var R = D.reconciliation, ST = [["MATCHED", "Matched"], ["TIMING_DIFFERENCE", "Late clearing"], ["BANK_ONLY", "Bank only"],
    ["LEDGER_ONLY", "In transit"], ["UNMATCHED_BANK", "Unmatched bank"], ["UNMATCHED_LEDGER", "Unmatched ledger"]];
  barChart($("c-recon"), {label: "Reconciliation items by status", color: "var(--series-1)", labelWidth: 118, fmt: count,
    rows: ST.map(function (s) { return {label: s[1], value: R.items[s[0]] || 0}; })});
  table($("t-recon-days"), [
    {label: "Account-days", get: function (r) { return r[1]; }},
    {label: "Count", num: 1, get: function (r) { return count(R.days[r[0]] || 0); }}
  ], [["RECONCILED", "Reconciled (statement = ledger)"], ["RECONCILING_ITEM", "Difference fully explained"], ["BREAK", "Unexplained break"]]);
  table($("t-breaks"), [
    {label: "Account", get: function (b) { return b.account; }},
    {label: "Date", get: function (b) { return b.date; }},
    {label: "Unexplained", num: 1, get: function (b) { return minor(b.unexplained, b.currency); }}
  ], R.breaks, {empty: "No unexplained breaks."});

  var det = D.detection.by_type, types = Object.keys(det).filter(function (k) { return k !== "ALL"; }).sort();
  var all = det.ALL;
  $("det-note").textContent = all.tp + " of " + all.labels + " planted problems found, " + all.fp + " false alarms; " +
    D.detection.clean_false_positives + " findings on the clean data.";
  var nice = function (s) { return (s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ")).replace(/^Fx /, "FX "); };
  table($("t-detection"), [
    {label: "Problem", get: function (k) { return k === "ALL" ? "All" : nice(k); }},
    {label: "Planted", num: 1, get: function (k) { return det[k].labels; }},
    {label: "Found", num: 1, get: function (k) { return det[k].tp; }},
    {label: "False alarms", num: 1, get: function (k) { return det[k].fp; }},
    {label: "Missed", num: 1, get: function (k) { return det[k].fn; }}
  ], types.concat(["ALL"]));

  var E = D.exceptions, byIssue = {};
  E.forEach(function (e) { byIssue[e.issue] = (byIssue[e.issue] || 0) + e.n; });
  barChart($("c-exceptions"), {label: "Exception log entries by issue", color: "var(--series-1)", labelWidth: 150, fmt: count,
    rows: Object.keys(byIssue).sort().map(function (k) { return {label: nice(k), value: byIssue[k]}; })});
  table($("t-exceptions"), [
    {label: "Rule", get: function (e) { return e.rule; }}, {label: "Issue", get: function (e) { return nice(e.issue); }},
    {label: "Severity", get: function (e) { return nice(e.severity); }}, {label: "Decision", get: function (e) { return nice(e.decision); }},
    {label: "Entries", num: 1, get: function (e) { return count(e.n); }}
  ], E);

  var STAGE = {baseline_inputs_before_injection: "Clean inputs, before injection", baseline: "Clean data + all outputs", injected: "Injected data + all outputs"};
  table($("t-validation"), [
    {label: "Stage", get: function (v) { return STAGE[v.stage] || v.stage; }},
    {label: "Passed", num: 1, get: function (v) { return v.passed; }},
    {label: "Failed", num: 1, get: function (v) { return v.failed; }},
    {label: "Failed checks", get: function (v) { return v.failed_checks.map(function (c) { return c.id + " (" + c.failures + ")"; }).join(", ") || "None"; }}
  ], D.validation);
}

function entityById(id) { return D.entities.filter(function (e) { return e.id === id; })[0]; }

function initEntity() {
  var sel = $("entity"), ids = runsAt(state.cutoff).filter(function (r) { return r.status !== "SUPERSEDED"; }).map(function (r) { return r.id; });
  var alerted = D.alerts.filter(function (a) { return a.level === "ENTITY" && ids.indexOf(a.run) >= 0; })
    .sort(function (a, b) { return (a.severity === "CRITICAL" ? 0 : 1) - (b.severity === "CRITICAL" ? 0 : 1) || a.min - b.min; });
  state.entity = alerted.length ? alerted[0].scope : D.entities[0].id;
  D.entities.forEach(function (e) {
    var o = h("option", {value: e.id}, e.id + " · " + e.name.replace(/^.*\(/, "(") + " " + e.currency, sel);
    if (e.id === state.entity) o.selected = true;
  });
  sel.addEventListener("change", function () { state.entity = sel.value; renderEntity(); });
}

function renderEntity() {
  var e = entityById(state.entity), f = D.forecasts[state.cutoff];
  var gidx = {}; D.group.dates.forEach(function (d, i) { gidx[d] = i; });
  var actual = (D.entity_actual[e.id] || []);
  var series = [{name: "Actual", color: "var(--series-ink)", values: f.dates.map(function (d) { return gidx[d] == null ? null : actual[gidx[d]]; })}];
  SC.forEach(function (s) { if (f.entity[s] && f.entity[s][e.id]) series.push({name: SC_LABEL[s], color: SC_COLOR[s], values: f.entity[s][e.id]}); });
  $("ent-title").textContent = "Entity drill-down: " + e.id + ", " + e.name;
  var low = function (s) { var v = f.entity[s] && f.entity[s][e.id]; return v ? Math.min.apply(null, v) : null; };
  var alerts = D.alerts.filter(function (a) { return a.scope === e.id && runsAt(state.cutoff).some(function (r) { return r.id === a.run && r.status !== "SUPERSEDED"; }); });
  $("ent-note").textContent = "Functional currency " + e.currency + " · minimum cash policy " + usd(e.min) +
    " · lowest projected: base " + usd(low("BASE")) + ", stress " + usd(low("STRESS")) +
    (alerts.length ? " · " + alerts.length + " " + alerts[0].severity.toLowerCase() + " alert at this cutoff" : " · no alert at this cutoff");
  legend($("l-entity"), series.map(function (s) { return {name: s.name, color: s.color}; }).concat([{name: "Entity minimum", ref: true}]));
  var ticks = [];
  for (var i = 0; i < f.dates.length; i += 7) ticks.push(i);
  lineChart($("c-entity"), {dates: f.dates, xticks: ticks, label: "Projected and actual cash for " + e.id,
    series: series, refs: [{name: "Minimum", value: e.min}]});
  table($("t-entity"), [{label: "Date", get: function (d) { return d; }}].concat(series.map(function (s) {
    return {label: s.name, num: 1, get: function (d) { return usd(s.values[f.dates.indexOf(d)], 2); }};
  })), f.dates);
  var scen = ["DELAYED_RECEIPT", "STRESS"].filter(function (s) { return f.effects[s]; });
  var cell = function (s, k) { var x = (f.effects[s][e.id] || {})[k]; return x ? x : 0; };
  var rows = EFFECT_ORDER.map(function (k) { return {label: EFFECT_LABEL[k], key: k}; });
  rows.push({label: "Total", key: null});
  table($("t-effects"), [{label: "Assumption", get: function (r) { return r.label; }}].concat(scen.map(function (s) {
    return {label: SC_LABEL[s], num: 1, get: function (r) {
      var v = r.key ? cell(s, r.key) : EFFECT_ORDER.reduce(function (t, k) { return t + cell(s, k); }, 0);
      return v ? usd(v, 2) : "–";
    }};
  })), rows);
}

function renderTop() {
  var rows = D.forecasts[state.cutoff].top_errors, CAT = {TIMING: "Timing", AMOUNT: "Amount", UNFORECAST: "Unforecast", ANOMALY: "Bad data excluded"};
  table($("t-top"), [
    {label: "#", num: 1, get: function (r) { return rows.indexOf(r) + 1; }},
    {label: "Cause", get: function (r) { return CAT[r.category] || r.category; }},
    {label: "Source", get: function (r) {
      var w = document.createElement("div"); h("div", {"class": "nw"}, r.source_id, w);
      if (r.detail) h("div", {"class": "note"}, r.detail, w);
      return w;
    }},
    {label: "Account", get: function (r) { return r.account; }},
    {label: "From", nowrap: 1, get: function (r) { return r.date; }},
    {label: "Contribution", num: 1, get: function (r) { return usd(r.usd, 2); }},
    {label: "What it is", get: function (r) { return r.explanation; }}
  ], rows, {empty: "No contributors at this cutoff."});
}

function renderScoped() { renderTiles(); renderHistory(); renderForecast(); renderVariance(); renderEntity(); renderTop(); renderRuns(); renderAlerts(); hideTip(); }

renderMeta();
initEntity();
renderScoped();
renderPeriod();
var lastW = window.innerWidth, timer = null;
window.addEventListener("resize", function () {
  if (window.innerWidth === lastW) return;
  lastW = window.innerWidth;
  clearTimeout(timer);
  timer = setTimeout(function () { renderHistory(); renderForecast(); renderVariance(); renderEntity(); renderPeriod(); }, 120);
});
})();
</script>
</body>
</html>
"""
