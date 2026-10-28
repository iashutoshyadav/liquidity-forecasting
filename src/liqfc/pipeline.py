"""End-to-end orchestration: generate -> validate baseline -> inject -> forecast -> detect -> evaluate -> export."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from liqfc.anomalies.injectors import inject_all
from liqfc.audit.events import AuditTrail, RunLifecycle
from liqfc.core.schema import INPUT_TABLES
from liqfc.engine.evaluation import data_quality_metrics, detection_metrics, forecast_metrics, liquidity_metrics
from liqfc.exporters.csv_json import write_plain_csv, write_tables, write_text
from liqfc.exporters.dashboard import build_dashboard
from liqfc.exporters.data_dictionary import render as render_dictionary
from liqfc.exporters.manifest import write_manifest
from liqfc.exporters.sql_seed import ddl, seed
from liqfc.validation.report import run_validation
from liqfc.core.calendar import BusinessCalendar
from liqfc.core.dataset import Dataset
from liqfc.engine.anomaly_detection import detect_all, flagged_cf_ids
from liqfc.engine.approval import material_breaks, run_holds
from liqfc.engine.balances import consolidated_positions, daily_cash_positions, split_ledger_by_currency
from liqfc.engine.forecast_run import execute_all
from liqfc.engine.fx_conversion import FxLookup
from liqfc.engine.liquidity_alerts import build_alerts
from liqfc.engine.scenarios import build_scenario_tables
from liqfc.engine.variance import attribute_run, compare_run
from liqfc.exporters.csv_json import canonical_frame, dataset_hash
from liqfc.generators.accounts import generate_accounts
from liqfc.generators.actual_cash_flows import build_actuals
from liqfc.generators.balance_statements import build_bank_lines, build_statements
from liqfc.generators.context import GenContext
from liqfc.generators.entities import generate_entities
from liqfc.generators.forecast_items import assign_cf_ids, build_revisions, finalize_cf
from liqfc.generators.fx_rates import fx_source_note, generate_rates
from liqfc.generators.ledger import build_ledger
from liqfc.generators.payables import build_payable_events, generate_payables, recurring_item_count
from liqfc.generators.receivables import build_receivable_events, generate_receivables


def item_counts(settings, n_entities: int) -> dict[str, int]:
    sc = settings.scale
    recurring = recurring_item_count(settings, n_entities)
    fixed = recurring + sc["one_off_payments"] + 2 * sc["ic_pairs"] + 2 * sc["instalment_receivables"]
    variable = sc["forecast_items"] - fixed
    if variable <= 0:
        raise ValueError("forecast_items too small for recurring/fixed items")
    n_rec = int(round(variable * settings.gen["receivable"]["receivable_share_of_variable_items"]))
    return {"recurring": recurring, "receivables_single": n_rec, "suppliers": variable - n_rec,
            "instalment": sc["instalment_receivables"], "ic_pairs": sc["ic_pairs"], "one_off": sc["one_off_payments"]}


def generate_baseline(settings) -> tuple[Dataset, GenContext]:
    """Generate the clean baseline in dependency order (parents before children)."""
    ctx = GenContext.create(settings)
    entities = generate_entities(ctx)
    accounts, account_events = generate_accounts(ctx, entities)
    rates = generate_rates(ctx)
    ctx.fx = FxLookup(rates, ctx.cal, settings.reporting_currency, settings.raw["fx"]["max_fallback_business_days"])
    scenarios, assumptions = build_scenario_tables(settings)
    n = item_counts(settings, len(entities))
    rec, rec_items, rec_settle, rec_plan, ic_pay = generate_receivables(
        ctx, entities, accounts, n["receivables_single"], n["instalment"], n["ic_pairs"])
    pay, pay_items, pay_settle = generate_payables(ctx, entities, accounts, n["suppliers"], n["one_off"], ic_pay)
    cf, links = assign_cf_ids(ctx, pd.concat([rec_items, pay_items], ignore_index=True))
    ledger, accounts, cash_events = build_ledger(ctx, accounts, pd.concat([rec_settle, pay_settle], ignore_index=True))
    actuals = build_actuals(ctx, ledger, cf)
    rec_events, rec = build_receivable_events(ctx, rec, actuals, rec_plan)
    pay_events, pay = build_payable_events(ctx, pay, actuals)
    cf = finalize_cf(cf, actuals, rec, pay)
    revisions, cf = build_revisions(ctx, cf, actuals)
    bank_lines = build_bank_lines(ctx, accounts, cash_events)
    statements = build_statements(ctx, accounts, bank_lines)
    ds = Dataset({
        "legal_entity": entities, "bank_account": accounts, "account_status_event": account_events,
        "currency_rate": rates, "liquidity_scenario": scenarios, "scenario_assumption": assumptions,
        "receivable": rec, "receivable_status_event": rec_events, "payable": pay, "payable_status_event": pay_events,
        "ledger_entry": ledger, "actual_cash_flow": actuals, "account_balance_statement": statements,
        "bank_statement_line": bank_lines,
        "cash_flow_forecast": cf, "cash_flow_forecast_revision": revisions, "forecast_source_link": links,
    }, label="baseline")
    return ds.conform(), ctx


def make_fx(ds: Dataset, settings) -> tuple[BusinessCalendar, FxLookup]:
    cal = BusinessCalendar(settings.raw["calendar"]["holidays"])
    return cal, FxLookup(ds["currency_rate"], cal, settings.reporting_currency, settings.raw["fx"]["max_fallback_business_days"])


@dataclass
class SystemResult:
    label: str
    derived: dict[str, pd.DataFrame]
    detections: pd.DataFrame
    positions_raw: pd.DataFrame
    run_specs: list
    attribution_raw: pd.DataFrame
    comparison_raw: pd.DataFrame
    run_accounts_raw: pd.DataFrame
    run_holds: dict = field(default_factory=dict)


def run_system(ds: Dataset, settings) -> SystemResult:
    """Calculate positions, detect anomalies, execute forecast runs and compare with actuals."""
    cal, fx = make_fx(ds, settings)
    positions, recon_items = daily_cash_positions(ds, settings, fx)
    consolidated = consolidated_positions(positions, ds["legal_entity"], settings.reporting_currency)
    detections = detect_all(ds, positions, cal, settings)
    flagged = flagged_cf_ids(detections)
    input_hash = dataset_hash(ds.input_only().tables)
    fc, specs = execute_all(ds, settings, cal, fx, input_hash)

    code_of = {s.run_id: s.code for s in specs}
    valid_ledger, _ = split_ledger_by_currency(ds["ledger_entry"], ds["bank_account"])
    atts, comps = [], []
    for _, run in fc["forecast_run"].iterrows():
        att = attribute_run(run, fc["forecast_run_line"], valid_ledger, ds["actual_cash_flow"],
                            ds["cash_flow_forecast"], flagged, settings.end)
        atts.append(att)
        comps.append(compare_run(run, code_of[run["forecast_run_id"]], fc["forecast_position"], fc["forecast_run_account"],
                                 positions, att, settings))
    attribution = pd.concat(atts, ignore_index=True)
    comparison = pd.concat(comps, ignore_index=True)
    alerts = build_alerts(fc["forecast_consolidated_position"], settings.id_year)

    lifecycle = RunLifecycle(settings.id_year)
    runs = fc["forecast_run"].set_index("forecast_run_id")
    breaks = material_breaks(positions, settings)
    ra = fc["forecast_run_account"]
    holds: dict[str, list[str]] = {}
    for spec in specs:
        gen = runs.at[spec.run_id, "generated_at"]
        lifecycle.transition(spec.run_id, "DRAFT", gen, spec.actor, "Run requested")
        lifecycle.transition(spec.run_id, "EXECUTED", gen + pd.Timedelta(seconds=1), "SYSTEM", "Forecast calculated")
        if spec.supersedes:
            lifecycle.transition(spec.supersedes, "SUPERSEDED", gen + pd.Timedelta(seconds=2), spec.actor,
                                 f"Superseded by {spec.run_id}: {spec.reason}")
        reasons = run_holds(spec, ra, breaks, settings)
        if reasons:
            holds[spec.run_id] = reasons
        else:
            lifecycle.transition(spec.run_id, "APPROVED", gen + pd.Timedelta(seconds=30), "RISK_REVIEWER",
                                 "No open review exceptions affect run inputs")
    run_events = lifecycle.frame()
    fc["forecast_run"]["run_status"] = fc["forecast_run"]["forecast_run_id"].map(lifecycle.status)

    derived = {
        "daily_cash_position": positions, "bank_reconciliation_item": recon_items,
        "consolidated_cash_position": consolidated, **fc,
        "forecast_comparison": comparison, "variance_attribution": attribution, "liquidity_alert": alerts,
        "forecast_run_event": run_events,
    }
    derived = {k: canonical_frame(v, k) for k, v in derived.items()}
    return SystemResult(ds.label, derived, detections, positions, specs, attribution, comparison, fc["forecast_run_account"],
                        holds)


def build_exceptions(result: SystemResult, validation_failures: pd.DataFrame, settings) -> pd.DataFrame:
    """ExceptionLog = detector findings + forecast-guard exclusions + structural validation failures."""
    gts = settings.generation_ts
    det = result.detections.copy()
    det["source"] = "DETECTION"
    det["forecast_run_id"] = None
    det["detected_at"] = gts
    lines = result.derived["forecast_run_line"]
    ex = lines[lines["line_status"] == "EXCLUDED"]
    runs = result.derived["forecast_run"].set_index("forecast_run_id")
    rule = {"CUTOFF_LEAKAGE": ("CUT-001", "REJECTED", "HIGH"), "MIXED_CURRENCY": ("CCY-001", "REJECTED", "HIGH"),
            "DUPLICATE_CASH_FLOW": ("DUP-001", "AUTO_RESOLVED", "MEDIUM")}
    guard = pd.DataFrame({
        "rule_id": ex["exclusion_reason"].map(lambda r: rule[r][0]), "issue_type": ex["exclusion_reason"],
        "affected_table": "cash_flow_forecast", "affected_record_id": ex["cf_id"],
        "severity": ex["exclusion_reason"].map(lambda r: rule[r][2]),
        "observed_value": ex["adjusted_amount_minor"].astype(str), "expected_value": "excluded from run",
        "decision": ex["exclusion_reason"].map(lambda r: rule[r][1]),
        "details": "line " + ex["line_id"] + " excluded by forecast guard", "source": "FORECAST_GUARD",
        "forecast_run_id": ex["forecast_run_id"], "detected_at": ex["forecast_run_id"].map(runs["generated_at"]),
    })
    val = validation_failures.copy()
    if len(val):
        val["source"] = "VALIDATION"
        val["forecast_run_id"] = None
        val["detected_at"] = gts + pd.Timedelta(hours=2)
    parts = [p for p in (det, guard, val) if len(p)]
    out = pd.concat(parts, ignore_index=True) if parts else det.assign(source=None, forecast_run_id=None, detected_at=None)
    out["resolution_status"] = [
        "RESOLVED" if (d == "AUTO_RESOLVED" or s == "FORECAST_GUARD") else "OPEN" for d, s in zip(out["decision"], out["source"])]
    order = {"DETECTION": 0, "FORECAST_GUARD": 1, "VALIDATION": 2}
    out["_o"] = out["source"].map(order)
    out = out.sort_values(["_o", "rule_id", "affected_table", "affected_record_id", "forecast_run_id"],
                          kind="mergesort", na_position="first").drop(columns="_o").reset_index(drop=True)
    out.insert(0, "exception_id", [f"EXC-{settings.id_year}-{i:06d}" for i in range(1, len(out) + 1)])
    return out


def build_audit(result: SystemResult, exceptions: pd.DataFrame, validation_summary: dict, settings) -> pd.DataFrame:
    trail = AuditTrail(settings.id_year)
    runs = result.derived["forecast_run"]
    spec_by = {s.run_id: s for s in result.run_specs}
    for r in runs.itertuples(index=False):
        trail.record(r.generated_at, spec_by[r.forecast_run_id].actor, "FORECAST_RUN_CREATED", "forecast_run",
                     r.forecast_run_id, scenario_id=r.scenario_id, cutoff=f"{r.cutoff_date:%Y-%m-%d}", input_hash=r.input_hash)
    for e in result.derived["forecast_run_event"].itertuples(index=False):
        trail.record(e.event_ts, e.actor_role, "FORECAST_RUN_STATUS_CHANGED", "forecast_run", e.forecast_run_id,
                     from_status=e.from_status, to_status=e.to_status)
    for x in exceptions.itertuples(index=False):
        trail.record(x.detected_at, "SYSTEM", "EXCEPTION_RAISED", "exception_log", x.exception_id,
                     rule_id=x.rule_id, decision=x.decision)
    for a in result.derived["liquidity_alert"].itertuples(index=False):
        ts = runs.set_index("forecast_run_id").at[a.forecast_run_id, "generated_at"] + pd.Timedelta(seconds=3)
        trail.record(ts, "SYSTEM", "LIQUIDITY_ALERT_RAISED", "liquidity_alert", a.alert_id, scope=a.scope_id, severity=a.severity)
    gen_at = runs.set_index("forecast_run_id")["generated_at"]
    for rid, reasons in sorted(result.run_holds.items()):
        trail.record(gen_at[rid] + pd.Timedelta(seconds=30), "SYSTEM", "FORECAST_RUN_HELD", "forecast_run", rid,
                     policy="RUN-01", reasons=reasons)
    trail.record(settings.generation_ts + pd.Timedelta(hours=2), "SYSTEM", "VALIDATION_COMPLETED", "dataset",
                 result.label, passed=validation_summary.get("passed"), failed=validation_summary.get("failed"))
    return trail.frame()


STAGES = ("generate", "forecast", "validate", "all")


def run_pipeline(settings, out_dir: Path, stage: str = "all", command: str | None = None, log=print) -> dict:
    """Run stages up to ``stage`` (earlier stages run automatically) and write outputs to ``out_dir``."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage}")
    upto = STAGES.index(stage)
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    command = command or f"python -m liqfc.cli {stage} --scale {settings.scale_name} --seed {settings.seed}"
    info: dict = {"scale": settings.scale_name, "seed": settings.seed}

    log(f"[generate] building clean baseline ({settings.scale_name})")
    log(f"[generate] {fx_source_note(settings)}")
    baseline, _ = generate_baseline(settings)
    cal, fx = make_fx(baseline, settings)
    names = list(INPUT_TABLES)
    sql_base = (ddl(names), seed(baseline.tables, names), {n: len(baseline[n]) for n in names})
    log("[validate] validating clean baseline inputs before injection")
    base_input_summary, _ = run_validation("baseline_inputs", baseline.tables, settings, cal, sql=sql_base, check_volume=True)
    if base_input_summary["failed"]:
        raise RuntimeError(f"clean baseline failed validation: {[c['check_id'] for c in base_input_summary['failed_checks']]}")
    log("[generate] injecting labelled anomalies")
    injected, gt = inject_all(baseline, settings, cal, fx)

    exp = settings.raw["export"]
    write_tables(baseline.tables, out / "baseline" / "input", exp["baseline_formats"])
    write_tables(injected.tables, out / "dataset" / "input", exp["dataset_formats"])
    write_text(out / "dataset" / "sql" / "schema.sql", ddl(names))
    write_text(out / "dataset" / "sql" / "seed.sql", seed(injected.tables, names))
    write_plain_csv(gt, out / "ground_truth" / "ground_truth.csv")
    write_text(out / "ground_truth" / "ground_truth.json", gt.to_json(orient="records", indent=1, force_ascii=False) + "\n")
    write_text(out / "reports" / "data_dictionary.md", render_dictionary())
    info["rows"] = {"baseline": baseline.row_counts(), "injected": injected.row_counts(), "ground_truth": len(gt)}

    if upto >= 1:
        log("[forecast] executing forecast runs on baseline and injected datasets")
        res_base = run_system(baseline, settings)
        res_inj = run_system(injected, settings)
    if upto >= 2:
        log("[validate] validating baseline and injected datasets with derived outputs")
        sql_inj = (ddl(names), seed(injected.tables, names), {n: len(injected[n]) for n in names})
        v_base, f_base = run_validation("baseline", baseline.tables, settings, cal, res_base.derived, sql_base, check_volume=True)
        v_inj, f_inj = run_validation("injected", injected.tables, settings, cal, res_inj.derived, sql_inj)
        for res, f, v in [(res_base, f_base, v_base), (res_inj, f_inj, v_inj)]:
            exc = build_exceptions(res, f, settings)
            res.derived["exception_log"] = canonical_frame(exc, "exception_log")
            res.derived["audit_event"] = canonical_frame(build_audit(res, exc, v, settings), "audit_event")
        summary = {"baseline_inputs_before_injection": base_input_summary, "baseline": v_base, "injected": v_inj}
        write_text(out / "reports" / "validation_summary.json", json.dumps(summary, indent=2, default=str) + "\n")
        info["validation"] = {k: {"passed": v["passed"], "failed": v["failed"]} for k, v in summary.items()}
    if upto >= 1:
        write_tables(res_base.derived, out / "baseline" / "derived", exp["baseline_formats"])
        write_tables(res_inj.derived, out / "dataset" / "derived", exp["dataset_formats"], exp["derived_json_max_rows"])
    if upto >= 3:
        log("[evaluate] computing evaluation metrics")
        ev = evaluate(settings, baseline, injected, gt, res_base, res_inj, v_base, v_inj)
        write_text(out / "reports" / "evaluation_summary.json", json.dumps(ev, indent=2, default=str) + "\n")
        write_text(out / "reports" / "evaluation_report.md", render_evaluation_md(ev, info, settings))
        info["evaluation"] = {"detection_all": ev["detection"]["injected"]["ALL"]}
        log("[dashboard] writing reports/dashboard.html")
        build_dashboard(out, settings)
    write_manifest(out, settings, command)
    log(f"[done] outputs written to {out}")
    return info


def evaluate(settings, baseline, injected, gt, res_base, res_inj, v_base, v_inj) -> dict:
    def exc_counts(res):
        e = res.derived["exception_log"]
        return e.groupby(["source", "issue_type", "decision"]).size().rename("n").reset_index().to_dict("records")

    return {
        "detection": {"injected": detection_metrics(res_inj.detections, gt),
                      "baseline_false_positives": int(len(res_base.detections))},
        "forecast_accuracy": {
            "baseline": forecast_metrics(res_base.derived["forecast_comparison"], res_base.derived["forecast_run"], settings),
            "injected": forecast_metrics(res_inj.derived["forecast_comparison"], res_inj.derived["forecast_run"], settings)},
        "data_quality": {"baseline": data_quality_metrics(v_base, res_base.derived["daily_cash_position"]),
                         "injected": data_quality_metrics(v_inj, res_inj.derived["daily_cash_position"])},
        "liquidity": {
            "baseline": liquidity_metrics(res_base.derived["forecast_run"], res_base.derived["liquidity_alert"],
                                          baseline["liquidity_scenario"]),
            "injected": liquidity_metrics(res_inj.derived["forecast_run"], res_inj.derived["liquidity_alert"],
                                          injected["liquidity_scenario"])},
        "exceptions": {"baseline": exc_counts(res_base), "injected": exc_counts(res_inj)},
    }


def render_evaluation_md(ev: dict, info: dict, settings) -> str:
    L = [f"# Evaluation report ({settings.scale_name}, seed {settings.seed})", "",
         "All figures are computed from generated outputs by `liqfc.engine.evaluation`; nothing is hard-coded.", "",
         "## Validation", "", "| Stage | Passed | Failed |", "|---|---|---|"]
    L += [f"| {k} | {v['passed']} | {v['failed']} |" for k, v in info.get("validation", {}).items()]
    L += ["", "Failures in the injected stage are expected: they are the labelled anomalies.", "",
          "## Anomaly detection (injected dataset vs ground truth)", "",
          "| Issue type | Labels | Detections | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|---|---|"]
    for k, m in ev["detection"]["injected"].items():
        L.append(f"| {k} | {m['labels']} | {m['detections']} | {m['tp']} | {m['fp']} | {m['fn']} | {m['precision']} | "
                 f"{m['recall']} | {m['f1']} |")
    L += ["", f"Detections on the clean baseline (false positives): {ev['detection']['baseline_false_positives']}", "",
          "## Forecast accuracy (account level, reporting currency major units, baseline dataset)", "",
          "| Slice | Rows | MAE | RMSE | Bias | MAPE % |", "|---|---|---|---|---|---|"]
    fa = ev["forecast_accuracy"]["baseline"]["account_level"]
    rows = [("overall", fa["overall"])] + [(f"scenario {k}", v) for k, v in fa["by_scenario"].items()] + \
           [(f"horizon {k}", v) for k, v in fa["by_horizon"].items()]
    L += [f"| {n} | {m['rows']} | {m['mae']} | {m['rmse']} | {m['bias']} | {m['mape_pct']} |" for n, m in rows]
    L += ["", "## Variance attribution at horizon end (baseline, reporting currency)", ""]
    for k, v in ev["forecast_accuracy"]["baseline"]["variance_attribution_at_horizon_end"].items():
        L.append(f"- **{k}** total {v['total_variance']}: " + ", ".join(f"{c} {x}" for c, x in v["components"].items())
                 + f" (reconciles: {v['components_reconcile']}, max |residual| {v['max_abs_residual_minor']} minor units)")
    L += ["", "## Data quality", "", "| Metric | Baseline | Injected |", "|---|---|---|"]
    for k in ev["data_quality"]["baseline"]:
        L.append(f"| {k} | {ev['data_quality']['baseline'][k]} | {ev['data_quality']['injected'][k]} |")
    L += ["", "## Liquidity (baseline)", "", "| Cutoff | BASE min | DELAYED min | STRESS min | BASE - STRESS |",
          "|---|---|---|---|---|"]
    for k, v in ev["liquidity"]["baseline"]["base_vs_stress_min_group_cash"].items():
        L.append(f"| {k} | {v['base_min']} | {v['delayed_min']} | {v['stress_min']} | {v['base_minus_stress']} |")
    L += ["", "| Run | Scenario | Min group cash | Threshold | First group breach | Entity alerts | First entity breach |",
          "|---|---|---|---|---|---|---|"]
    for k, v in ev["liquidity"]["baseline"]["per_run"].items():
        L.append(f"| {k} | {v['scenario']} | {v['min_projected_group_cash']} | {v['group_threshold']} | "
                 f"{v['first_group_breach_date']} | {v['entity_alerts']} | {v['first_entity_breach_date']} |")
    return "\n".join(L) + "\n"
