"""Business validation: reconciliation, FX coverage, currency validity, cutoff compliance,
scenario transformation correctness, position consistency and variance reconciliation.

Re-derivations here are written independently of the engine so they genuinely check it.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

import numpy as np
import pandas as pd

from liqfc.core import money
from liqfc.validation.checks import CheckResult


def _st(name, check_id, table, checked, fails, issue="BUSINESS_RULE_VIOLATION", emit=True, decision="HELD_FOR_REVIEW",
        severity="HIGH"):
    return CheckResult(check_id, "BUSINESS", name, table, checked, fails, issue, severity, decision, emit)


def input_business_checks(t: dict[str, pd.DataFrame], settings, cal) -> list[CheckResult]:
    out = []
    acc = t["bank_account"]
    acc_ccy = acc.set_index("account_id")["currency"]
    led = t["ledger_entry"]
    st = t["account_balance_statement"].sort_values(["account_id", "statement_date"])

    flows = led.groupby(["account_id", "value_date"])["amount_minor"].sum()
    s = st.set_index(["account_id", "statement_date"])
    moved = flows.reindex(s.index, fill_value=0)
    calc = s.index.get_level_values(0).map(acc.set_index("account_id")["opening_balance_minor"]).to_numpy(np.int64) + \
        moved.groupby(level=0).cumsum().to_numpy(np.int64)
    bad = calc != s["closing_balance_minor"].to_numpy(np.int64)
    out.append(_st("Statement closing = opening balance + cumulative ledger", "BUS-REC", "account_balance_statement",
                   len(s), [(i, o, c, "balance break") for i, o, c in zip(s.loc[bad, "statement_id"],
                                                                       s.loc[bad, "closing_balance_minor"], calc[bad])],
                   "BALANCE_MISMATCH", emit=False))
    arith = st["opening_balance_minor"] + st["total_credits_minor"] - st["total_debits_minor"] != st["closing_balance_minor"]
    cont = st.groupby("account_id")["closing_balance_minor"].shift(1)
    cont_bad = cont.notna() & (cont != st["opening_balance_minor"])
    out.append(_st("Statement arithmetic and day-to-day continuity", "BUS-STMT", "account_balance_statement", len(st),
                   [(i, "", "", "opening+credits-debits != closing") for i in st.loc[arith, "statement_id"]] +
                   [(i, "", "", "opening != previous closing") for i in st.loc[cont_bad, "statement_id"]],
                   "BALANCE_MISMATCH", emit=False))

    need = sorted(c for c in acc["currency"].unique() if c != settings.reporting_currency)
    bdays = pd.to_datetime(cal.business_days(settings.start, settings.end))
    have = set(zip(t["currency_rate"]["base_currency"], t["currency_rate"]["rate_date"]))
    miss = [(f"{c}@{d:%Y-%m-%d}", "missing", "rate", "FX coverage") for c in need for d in bdays if (c, d) not in have]
    out.append(_st("FX rate coverage (business days x currencies)", "BUS-FX", "currency_rate", len(need) * len(bdays), miss,
                   "FX_RATE_MISSING", emit=False))

    fails, checked = [], 0
    for table, key in [("ledger_entry", "ledger_entry_id"), ("actual_cash_flow", "actual_id"), ("receivable", "receivable_id"),
                       ("payable", "payable_id"), ("cash_flow_forecast", "cf_id"), ("account_balance_statement", "statement_id")]:
        d = t[table]
        checked += len(d)
        b = d["currency"].values != d["account_id"].map(acc_ccy).values
        fails += [(f"{table}:{k}", c, acc_ccy[a], "currency != account currency")
                  for k, c, a in zip(d.loc[b, key], d.loc[b, "currency"], d.loc[b, "account_id"])]
    out.append(_st("Currency equals account currency (no unconverted mixing)", "BUS-CCY", "multiple", checked, fails,
                   "MIXED_CURRENCY", emit=False))

    links = t["forecast_source_link"]
    tot = links.groupby(["source_type", "source_id"])["allocation_bp"].sum()
    over = tot[tot > 10000]
    out.append(_st("Source allocation <= 100% (no double counting)", "BUS-DUPSRC", "forecast_source_link", len(tot),
                   [(f"{a}:{b}", v, "<=10000", "source over-allocated") for (a, b), v in over.items()],
                   "DUPLICATE_CASH_FLOW", emit=False))

    cf = t["cash_flow_forecast"]
    act = t["actual_cash_flow"].set_index("actual_id")
    b = cf[cf["basis_actual_id"].notna()]
    rec = b["basis_actual_id"].map(act["recorded_at"])
    bad = rec.isna() | (rec >= b["known_at"])
    out.append(_st("Forecast item basis recorded before item known", "BUS-CUT-ITEM", "cash_flow_forecast", len(b),
                   [(i, r, k, "basis after known_at") for i, r, k in zip(b.loc[bad, "cf_id"], rec[bad], b.loc[bad, "known_at"])],
                   "CUTOFF_LEAKAGE", emit=False))

    l = led.set_index("ledger_entry_id")
    a = t["actual_cash_flow"]
    m = (a["amount_minor"].values != a["ledger_entry_id"].map(l["amount_minor"]).values) | \
        (a["actual_date"].values != a["ledger_entry_id"].map(l["value_date"]).values) | \
        (a["recorded_at"].values != a["ledger_entry_id"].map(l["recorded_at"]).values) | \
        (a["account_id"].values != a["ledger_entry_id"].map(l["account_id"]).values)
    out.append(_st("Actual cash flow equals its ledger entry", "BUS-ACT", "actual_cash_flow", len(a),
                   [(i, "", "", "actual differs from ledger") for i in a.loc[m, "actual_id"]]))

    rec_ids = set(t["receivable"]["receivable_id"])
    sign_bad = cf[((cf["source_id"].isin(rec_ids)) & (cf["amount_minor"] <= 0)) | ((~cf["source_id"].isin(rec_ids)) & (cf["amount_minor"] >= 0))]
    own = []
    acc_ent = acc.set_index("account_id")["entity_id"]
    for table, key in [("receivable", "receivable_id"), ("payable", "payable_id"), ("cash_flow_forecast", "cf_id")]:
        d = t[table]
        bb = d["entity_id"].values != d["account_id"].map(acc_ent).values
        own += [(f"{table}:{k}", "", "", "account belongs to another entity") for k in d.loc[bb, key]]
    out.append(_st("Sign convention and account ownership", "BUS-SIGN", "cash_flow_forecast", len(cf) + len(t["receivable"]) + len(t["payable"]),
                   [(i, v, "sign by source", "wrong sign") for i, v in zip(sign_bad["cf_id"], sign_bad["amount_minor"])] + own))

    pay = t["payable"]
    ic = pay[pay["category"] == "INTERCOMPANY"].merge(t["receivable"], left_on="ic_receivable_id", right_on="receivable_id",
                                                      suffixes=("_p", "_r"))
    bad = ic[(ic["amount_minor_p"] != ic["amount_minor_r"]) | (ic["currency_p"] != ic["currency_r"]) |
             (ic["scheduled_date"] != ic["due_date"]) | (ic["counterparty_entity_id_p"] != ic["entity_id_r"]) |
             (ic["counterparty_entity_id_r"] != ic["entity_id_p"])]
    out.append(_st("Intercompany payable mirrors receivable", "BUS-IC", "payable", len(ic),
                   [(i, "", "", "intercompany mismatch") for i in bad["payable_id"]]))

    ev = t["account_status_event"]
    dorm = ev[ev["to_status"] == "DORMANT"].set_index("account_id")["event_date"]
    late = led[led["account_id"].isin(dorm.index) & (led["value_date"] >= led["account_id"].map(dorm))]
    out.append(_st("No ledger activity on dormant accounts", "BUS-DORMANT", "ledger_entry", len(led),
                   [(i, "", "", "activity after dormancy") for i in late["ledger_entry_id"]]))
    return out


def volume_check(t: dict[str, pd.DataFrame], settings) -> CheckResult:
    sc = settings.scale
    targets = {"legal_entity": sc["legal_entities"], "bank_account": sc["bank_accounts"],
               "ledger_entry": sc["ledger_entries"], "cash_flow_forecast": sc["forecast_items"]}
    fails = [(k, len(t[k]), v, "volume target") for k, v in targets.items() if len(t[k]) != v]
    ccys = set(t["bank_account"]["currency"])
    if ccys != set(settings.minor_units):
        fails.append(("bank_account.currency", sorted(ccys), sorted(settings.minor_units), "currency coverage"))
    return _st("Configured volumes and five-currency coverage", "BUS-VOL", "multiple", len(targets) + 1, fails)


def derived_checks(t: dict[str, pd.DataFrame], d: dict[str, pd.DataFrame], settings, cal) -> list[CheckResult]:
    out = []
    mu, rep = settings.minor_units, settings.reporting_currency
    p = d["daily_cash_position"].sort_values(["account_id", "position_date"])
    arith = p["opening_balance_minor"] + p["credits_minor"] - p["debits_minor"] != p["closing_balance_minor"]
    prev = p.groupby("account_id")["closing_balance_minor"].shift(1)
    first = p.groupby("account_id").cumcount() == 0
    op0 = p["account_id"].map(t["bank_account"].set_index("account_id")["opening_balance_minor"])
    cont = (~first & (prev != p["opening_balance_minor"])) | (first & (op0 != p["opening_balance_minor"]))
    conv = p[p["fx_rate"].notna()]
    reconv = [money.convert(int(a), mu[c], money.parse_rate(r), mu[rep]) for a, c, r in
              zip(conv["closing_balance_minor"], conv["currency"], conv["fx_rate"])]
    conv_bad = conv[np.array(reconv, dtype=object) != conv["closing_balance_reporting_minor"].astype(object).to_numpy()]
    ent = p.groupby(["entity_id", "position_date"])["closing_balance_reporting_minor"].sum()
    c = d["consolidated_cash_position"]
    ce = c[c["level"] == "ENTITY"].set_index(["scope_id", "position_date"])["closing_balance_reporting_minor"]
    cg = c[c["level"] == "GROUP"].set_index("position_date")["closing_balance_reporting_minor"]
    ent_bad = (ent.reindex(ce.index).fillna(0).astype(np.int64) != ce)
    grp_bad = ce.groupby(level=1).sum().reindex(cg.index) != cg
    fails = [(f"{a}|{x:%Y-%m-%d}", "", "", "opening+credits-debits != closing")
             for a, x in zip(p.loc[arith, "account_id"], p.loc[arith, "position_date"])]
    fails += [(f"{a}|{x:%Y-%m-%d}", "", "", "opening != previous closing")
              for a, x in zip(p.loc[cont, "account_id"], p.loc[cont, "position_date"])]
    fails += [(f"{a}|{x:%Y-%m-%d}", "", "", "reporting conversion mismatch")
              for a, x in zip(conv_bad["account_id"], conv_bad["position_date"])]
    fails += [(f"{k[0]}|{k[1]:%Y-%m-%d}", "", "", "entity != sum of accounts") for k in ent_bad[ent_bad].index]
    fails += [(f"GROUP|{k:%Y-%m-%d}", "", "", "group != sum of entities") for k in grp_bad[grp_bad].index]
    out.append(_st("Daily cash position consistency (account/entity/group)", "BUS-POS", "daily_cash_position", len(p), fails))

    runs = d["forecast_run"].set_index("forecast_run_id")
    lines = d["forecast_run_line"]
    inc = lines[lines["line_status"] == "INCLUDED"].copy()
    inc["cutoff"] = inc["forecast_run_id"].map(runs["cutoff_ts"])
    cf_known = t["cash_flow_forecast"].set_index("cf_id")["known_at"]
    act_rec = t["actual_cash_flow"].set_index("actual_id")["recorded_at"]
    basis_ts = inc["basis_record_id"].map(act_rec).fillna(inc["basis_record_id"].map(cf_known))
    known = inc["cf_id"].map(cf_known)
    bad = (inc["source_type"] != "SCENARIO") & ((basis_ts >= inc["cutoff"]) | (known >= inc["cutoff"]) | basis_ts.isna())
    fails = [(l, "", "", "line uses information recorded after cutoff") for l in inc.loc[bad, "line_id"]]
    ra = d["forecast_run_account"]
    led = t["ledger_entry"]
    for rid, g in ra.groupby("forecast_run_id"):
        cut = runs.at[rid, "cutoff_ts"]
        exp = led[led["recorded_at"] < cut].groupby("account_id")["amount_minor"].sum()
        exp = g["account_id"].map(exp).fillna(0).astype(np.int64) + g["account_id"].map(
            t["bank_account"].set_index("account_id")["opening_balance_minor"])
        fails += [(f"{rid}|{a}", o, e, "opening balance uses post-cutoff entries")
                  for a, o, e in zip(g["account_id"], g["opening_balance_minor"], exp) if o != e]
    out.append(_st("Forecast cutoff compliance", "BUS-CUTOFF-RUN", "forecast_run_line", len(inc) + len(ra), fails,
                   "CUTOFF_LEAKAGE", decision="REJECTED"))

    out.append(scenario_check(t, d, settings, cal))

    comp = d["forecast_comparison"]
    tol = int(settings.raw["money"]["variance_rounding_tolerance_minor"])
    loc_bad = comp["variance_minor"] != comp[["timing_minor", "amount_minor", "unforecast_minor", "anomaly_minor"]].sum(axis=1)
    calc_v = comp["actual_closing_minor"] - comp["forecast_closing_minor"]
    res = comp["residual_reporting_minor"].dropna().abs()
    att = d["variance_attribution"]
    end_rows = comp.merge(runs[["horizon_end_date"]], left_on="forecast_run_id", right_index=True)
    end_rows = end_rows[end_rows["position_date"] == end_rows["horizon_end_date"].clip(upper=settings.end)]
    tot = att.groupby(["forecast_run_id", "account_id", "category"])["amount_minor"].sum().unstack(fill_value=0)
    det_bad = []
    for r in end_rows.itertuples(index=False):
        k = (r.forecast_run_id, r.account_id)
        row = tot.loc[k] if k in tot.index else None
        for cat, val in [("TIMING", r.timing_minor), ("AMOUNT", r.amount_minor), ("UNFORECAST", r.unforecast_minor),
                         ("ANOMALY", r.anomaly_minor)]:
            exp = 0 if row is None or cat not in row else int(row[cat])
            if exp != val:
                det_bad.append((f"{k[0]}|{k[1]}|{cat}", val, exp, "comparison != sum of attribution detail"))
    fails = [(f"{a}|{b}|{c:%Y-%m-%d}", "", "", "components do not sum to variance")
             for a, b, c in zip(comp.loc[loc_bad, "forecast_run_id"], comp.loc[loc_bad, "account_id"], comp.loc[loc_bad, "position_date"])]
    fails += [(f"{a}|{b}", "", "", "variance != actual - forecast") for a, b in
              zip(comp.loc[calc_v != comp["variance_minor"], "forecast_run_id"], comp.loc[calc_v != comp["variance_minor"], "account_id"])]
    fails += [(f"residual#{i}", v, f"<= {tol}", "reporting residual above tolerance") for i, v in res[res > tol].items()]
    fails += det_bad
    out.append(_st("Variance attribution reconciles to total variance", "BUS-VAR", "forecast_comparison", len(comp), fails))

    fails = []
    for rid, r in runs.iterrows():
        if pd.notna(r["supersedes_run_id"]):
            if r["supersedes_run_id"] not in runs.index:
                fails.append((rid, r["supersedes_run_id"], "superseded run retained", "missing superseded run"))
            elif runs.at[r["supersedes_run_id"], "run_status"] != "SUPERSEDED":
                fails.append((r["supersedes_run_id"], runs.at[r["supersedes_run_id"], "run_status"], "SUPERSEDED", "not superseded"))
    out.append(_st("Forecast versions immutable and history preserved", "BUS-RUN-HIST", "forecast_run", len(runs), fails))
    return out


def _stable_unit(seed, code, key) -> float:
    return int.from_bytes(hashlib.sha256(f"{seed}|{code}|{key}".encode()).digest()[:8], "big") / 2**64


def scenario_check(t, d, settings, cal) -> CheckResult:
    runs = d["forecast_run"]
    scen = t["liquidity_scenario"].set_index("scenario_id")["scenario_code"]
    runs = runs.assign(code=runs["scenario_id"].map(scen))
    lines = d["forecast_run_line"].merge(t["cash_flow_forecast"][["cf_id", "flow_category"]], on="cf_id", how="left")
    fails, checked = [], 0
    for cut, g in runs[runs["supersedes_run_id"].isna()].groupby("cutoff_date"):
        base_id = g.loc[g["code"] == "BASE", "forecast_run_id"].iloc[0]
        base = lines[(lines["forecast_run_id"] == base_id) & lines["cf_id"].notna()].set_index("cf_id")
        for r in g[g["code"] != "BASE"].itertuples(index=False):
            p = settings.scenarios[r.code]
            prop = float(Decimal(str(p["receivable_delay"]["proportion"])))
            delay = int(p["receivable_delay"]["delay_days"])
            h = int(p["collection_haircut_bp"])
            sl = lines[lines["forecast_run_id"] == r.forecast_run_id]
            s_items = sl[sl["cf_id"].notna()].set_index("cf_id")
            if set(s_items.index) != set(base.index):
                fails.append((r.forecast_run_id, len(s_items), len(base), "scenario line set differs from BASE"))
                continue
            for cid, b in base.iterrows():
                s = s_items.loc[cid]
                checked += 1
                exp_date, exp_amt = b["base_date"], int(b["base_amount_minor"])
                if b["flow_category"] == "CUSTOMER_RECEIPT":
                    if prop > 0 and _stable_unit(settings.seed, r.code, cid) < prop and delay:
                        exp_date = pd.Timestamp(cal.roll_forward([b["base_date"] + pd.Timedelta(days=delay)])[0])
                    if h:
                        exp_amt = money.div_half_even(exp_amt * (10000 - h), 10000)
                if (s["base_date"] != b["base_date"] or int(s["base_amount_minor"]) != int(b["base_amount_minor"])
                        or s["adjusted_date"] != exp_date or int(s["adjusted_amount_minor"]) != exp_amt
                        or s["exclusion_reason"] != b["exclusion_reason"] and not (pd.isna(s["exclusion_reason"]) and pd.isna(b["exclusion_reason"]))):
                    fails.append((f"{r.forecast_run_id}|{cid}", f"{s['adjusted_date']:%Y-%m-%d}/{s['adjusted_amount_minor']}",
                                  f"{exp_date:%Y-%m-%d}/{exp_amt}", "undocumented scenario difference"))
            extra = sl[sl["cf_id"].isna()]
            want = 1 if p["one_off_payment"] else 0
            if len(extra) != want:
                fails.append((r.forecast_run_id, len(extra), want, "scenario-only line count"))
            ra = d["forecast_run_account"]
            for a in ra[ra["forecast_run_id"] == r.forecast_run_id].itertuples(index=False):
                shock = int((p["fx_shock_bp"] or {}).get(a.currency, 0))
                if a.fx_rate is not None and not pd.isna(a.fx_rate):
                    exp = money.div_half_even(money.parse_rate(a.fx_rate) * (10000 + shock), 10000)
                    if money.parse_rate(a.fx_rate_applied) != exp or a.fx_shock_bp != shock:
                        fails.append((f"{r.forecast_run_id}|{a.account_id}", a.fx_rate_applied, money.format_rate(exp), "fx shock"))
    return _st("Scenario output = documented transformation of BASE", "BUS-SCN", "forecast_run_line", checked, fails)
