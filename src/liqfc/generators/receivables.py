"""Receivable generation, settlement simulation and lifecycle events.

Rules (docs/business_rules.md, RCV-*):
* RCV-01 due_date = next business day on/after invoice_date + payment_terms_days.
* RCV-02 payment delay ~ Gamma(shape, scale) + shift by risk class; paid on the next business
  day on/after due_date + delay. Intercompany receivables settle exactly on due_date.
* RCV-03 a receivable never pays with the risk-class write-off probability; it becomes OVERDUE
  the day after due_date and WRITTEN_OFF ``write_off_after_days`` after due_date.
* RCV-04 partial payers pay ``partial_first_share_bp`` first, the rest ``partial_second_lag_days`` later.
* RCV-05 instalment receivables (2 forecast items, 50/50) are class A, gap = instalment_gap_days.
* RCV-06 OVERDUE is set the day after due_date only while nothing has been paid.
* RCV-08 with receivable_delay_profile=calibrated, classes A and B take their delay parameters and
  relative shares from data/external/ar_delay_calibration.json; class C (absent from that data)
  keeps the configured parameters and calibrated_class_c_weight.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from liqfc.core import ids, money
from liqfc.generators.context import GenContext


def delay_model(settings) -> tuple[dict[str, float], dict[str, dict]]:
    """Risk-class weights and shifted-gamma delay parameters for the active profile."""
    cfg = settings.gen["receivable"]
    weights = dict(cfg["risk_class_weights"])
    gamma = {k: dict(v) for k, v in cfg["delay_gamma"].items()}
    if settings.external["receivable_delay_profile"] != "calibrated":
        return weights, gamma
    cal = json.loads(settings.external_path("calibration_file").read_text(encoding="utf-8"))
    seg = cal["segments"]
    c_weight = float(settings.external["calibrated_class_c_weight"])
    share = sum(seg[k]["invoice_share"] for k in ("A", "B") if k in seg)
    for k in ("A", "B"):
        if k in seg:
            weights[k] = (1 - c_weight) * seg[k]["invoice_share"] / share
            gamma[k] = dict(seg[k]["fitted_delay_gamma"])
    weights["C"] = c_weight
    return weights, gamma


def _accounts_for(accounts: pd.DataFrame, entity: str, weights: dict[str, float]) -> tuple[list[str], np.ndarray]:
    sub = accounts[(accounts["entity_id"] == entity) & accounts["account_type"].isin(list(weights))]
    w = sub["account_type"].map(weights).to_numpy(dtype=float)
    counts = sub["account_type"].map(sub["account_type"].value_counts()).to_numpy(dtype=float)
    w = w / counts
    return list(sub["account_id"]), w / w.sum()


def generate_receivables(ctx: GenContext, entities: pd.DataFrame, accounts: pd.DataFrame,
                         n_single: int, n_inst: int, n_ic: int):
    s = ctx.settings
    cfg = s.gen["receivable"]
    rng = ctx.rng("receivables")
    cal = ctx.cal
    cal_cfg = s.raw["calendar"]
    window = cal.business_days(cal_cfg["invoice_window_start"], cal_cfg["invoice_window_end"])
    window_months = len(pd.date_range(cal_cfg["invoice_window_start"], cal_cfg["invoice_window_end"])) / 30.4
    acc_ccy = accounts.set_index("account_id")["currency"].to_dict()
    eids = entities["entity_id"].to_numpy()
    size = np.array([ctx.entity_size[e] for e in eids], dtype=float)

    n = n_single + n_inst
    ent = rng.choice(eids, size=n, p=size / size.sum())
    inst_flag = np.zeros(n, dtype=bool)
    inst_flag[rng.choice(n, size=n_inst, replace=False)] = True
    acct = np.empty(n, dtype=object)
    for e in np.unique(ent):
        idx = np.where(ent == e)[0]
        cands, w = _accounts_for(accounts, e, cfg["account_type_weights"])
        acct[idx] = rng.choice(cands, size=len(idx), p=w)
    ccy = np.array([acc_ccy[a] for a in acct], dtype=object)
    invoice = pd.to_datetime(window[rng.integers(0, len(window), n)])
    terms = rng.choice(cfg["payment_terms_days"], size=n, p=cfg["payment_terms_weights"])
    class_weights, _ = delay_model(s)
    classes = list(class_weights)
    risk = rng.choice(classes, size=n, p=[class_weights[c] for c in classes]).astype(object)
    risk[inst_flag] = "A"
    due1 = pd.to_datetime(cal.roll_forward(invoice + pd.to_timedelta(terms, unit="D")))
    due2 = pd.to_datetime(cal.roll_forward(due1 + pd.Timedelta(days=int(cfg["instalment_gap_days"]))))

    per_entity = pd.Series(ent).value_counts()
    mean_usd = np.array([ctx.entity_size[e] * window_months / per_entity[e] for e in ent])
    sigma = float(cfg["amount_sigma"])
    usd = np.exp(rng.normal(np.log(mean_usd) - sigma**2 / 2, sigma))
    amount = np.maximum(ctx.usd_to_local_minor(usd, ccy, invoice), 100)
    n_customers = max(20, n // 8)
    customer = np.array([f"CUST-{int(c):05d}" for c in rng.integers(1, n_customers + 1, n)], dtype=object)

    single_idx = np.where(~inst_flag)[0]
    k = int(s.scale["legit_same_amount_pairs"])
    pairs = rng.choice(single_idx, size=2 * k, replace=False).reshape(-1, 2) if k else np.empty((0, 2), int)
    for i, j in pairs:
        for arr in (ent, acct, ccy, terms, risk, customer, amount):
            arr[j] = arr[i]
        invoice = invoice.where(np.arange(n) != j, invoice[i])
        due1 = due1.where(np.arange(n) != j, due1[i])
        due2 = due2.where(np.arange(n) != j, due2[i])

    rec = pd.DataFrame({
        "entity_id": ent, "account_id": acct, "customer_id": customer, "counterparty_entity_id": None,
        "is_intercompany": False, "risk_class": risk, "invoice_date": invoice,
        "due_date": np.where(inst_flag, due2, due1), "x_due1": due1, "x_due2": due2,
        "payment_terms_days": terms.astype(int), "instalment_count": np.where(inst_flag, 2, 1),
        "currency": ccy, "amount_minor": amount.astype(np.int64),
    })
    rec["due_date"] = pd.to_datetime(rec["due_date"])

    ic_rows, ic_pay = [], []
    pref_rec = {"OPERATING", "COLLECTION"}
    pref_pay = {"OPERATING", "DISBURSEMENT"}
    usable = accounts[accounts["account_type"] != "RESERVE"]
    attempts = 0
    while len(ic_rows) < n_ic:
        attempts += 1
        if attempts > n_ic * 200:
            raise ValueError("could not build intercompany pairs with a shared currency")
        e1, e2 = rng.choice(eids, size=2, replace=False, p=size / size.sum())
        a1 = usable[usable["entity_id"] == e1]
        a2 = usable[usable["entity_id"] == e2]
        common = sorted(set(a1["currency"]) & set(a2["currency"]))
        if not common:
            continue
        c = str(rng.choice(common))
        r_c = a1[a1["currency"] == c]
        p_c = a2[a2["currency"] == c]
        r_acc = sorted(r_c[r_c["account_type"].isin(pref_rec)]["account_id"]) or sorted(r_c["account_id"])
        p_acc = sorted(p_c[p_c["account_type"].isin(pref_pay)]["account_id"]) or sorted(p_c["account_id"])
        inv = pd.Timestamp(window[int(rng.integers(0, len(window)))])
        due = pd.Timestamp(cal.roll_forward([inv + pd.Timedelta(days=30)])[0])
        usd_amt = float(np.exp(rng.normal(np.log(0.10 * ctx.entity_size[e1]) - 0.18, 0.6)))
        amt = int(max(ctx.usd_to_local_minor([usd_amt], [c], [inv])[0], 100))
        ic_rows.append({
            "entity_id": e1, "account_id": str(rng.choice(r_acc)), "customer_id": None, "counterparty_entity_id": e2,
            "is_intercompany": True, "risk_class": "A", "invoice_date": inv, "due_date": due, "x_due1": due,
            "x_due2": pd.NaT, "payment_terms_days": 30, "instalment_count": 1, "currency": c, "amount_minor": amt,
        })
        ic_pay.append({"entity_id": e2, "account_id": str(rng.choice(p_acc)), "counterparty_entity_id": e1,
                       "currency": c, "amount_minor": amt, "invoice_date": inv, "scheduled_date": due})
    ic = pd.DataFrame(ic_rows)
    ic["x_ic_index"] = np.arange(len(ic))
    rec["x_ic_index"] = -1
    rec = pd.concat([rec, ic], ignore_index=True)
    rec["x_draw"] = np.arange(len(rec))
    rec = rec.sort_values(["invoice_date", "entity_id", "account_id", "amount_minor", "x_draw"], kind="mergesort").reset_index(drop=True)
    rec.insert(0, "receivable_id", ids.seq_ids("RCV", s.id_year, len(rec)))
    ic_map = rec.loc[rec["x_ic_index"] >= 0].set_index("x_ic_index")["receivable_id"]
    for i, p in enumerate(ic_pay):
        p["ic_receivable_id"] = ic_map[i]

    items, settlements, plan = _simulate(ctx, rec)
    return rec, items, settlements, plan, ic_pay


def _simulate(ctx: GenContext, rec: pd.DataFrame):
    s = ctx.settings
    cfg = s.gen["receivable"]
    rng = ctx.rng("receivables:settlement")
    cal = ctx.cal
    end = s.end
    _, gammas = delay_model(s)
    items, settle, plan = [], [], []
    for r in rec.itertuples(index=False):
        flow = "INTERCOMPANY_RECEIPT" if r.is_intercompany else "CUSTOMER_RECEIPT"
        cpty = r.counterparty_entity_id if r.is_intercompany else r.customer_id
        g = gammas[r.risk_class]
        base = dict(source_type="RECEIVABLE", source_id=r.receivable_id, entity_id=r.entity_id,
                    account_id=r.account_id, currency=r.currency)
        never_pays = False
        payments = []
        if r.instalment_count == 2:
            first = r.amount_minor // 2
            parts = [(1, r.x_due1, first), (2, r.x_due2, r.amount_minor - first)]
            for no, due, amt in parts:
                items.append({**base, "instalment_no": no, "amount_minor": amt, "expected_date": due,
                              "known_date": r.invoice_date, "flow_category": flow, "allocation_bp": 5000})
                delay = int(round(rng.gamma(g["shape"], g["scale"]))) + int(g["shift"])
                payments.append((no, pd.Timestamp(cal.roll_forward([due + pd.Timedelta(days=delay)])[0]), amt))
        else:
            items.append({**base, "instalment_no": 1, "amount_minor": r.amount_minor, "expected_date": r.due_date,
                          "known_date": r.invoice_date, "flow_category": flow, "allocation_bp": 10000})
            if r.is_intercompany:
                payments.append((1, r.due_date, r.amount_minor))
            else:
                u_wo, u_part = rng.random(), rng.random()
                delay = int(round(rng.gamma(g["shape"], g["scale"]))) + int(g["shift"])
                if u_wo < cfg["write_off_probability"][r.risk_class]:
                    never_pays = True
                else:
                    pay = pd.Timestamp(cal.roll_forward([r.due_date + pd.Timedelta(days=delay)])[0])
                    if u_part < cfg["partial_payment_probability"]:
                        first = money.apply_bp(r.amount_minor, cfg["partial_first_share_bp"])
                        second = pd.Timestamp(cal.roll_forward([pay + pd.Timedelta(days=int(cfg["partial_second_lag_days"]))])[0])
                        payments += [(1, pay, first), (1, second, r.amount_minor - first)]
                    else:
                        payments.append((1, pay, r.amount_minor))
        for no, d, amt in payments:
            if s.start <= d <= end:
                settle.append({"source_type": "RECEIVABLE", "source_id": r.receivable_id, "instalment_no": no,
                               "account_id": r.account_id, "currency": r.currency, "amount_minor": int(amt),
                               "value_date": d, "entry_type": flow, "counterparty_ref": cpty})
        wo = r.due_date + pd.Timedelta(days=int(cfg["write_off_after_days"]))
        plan.append({"receivable_id": r.receivable_id, "never_pays": never_pays,
                     "writeoff_date": wo if never_pays and wo <= end else pd.NaT})
    return pd.DataFrame(items), pd.DataFrame(settle), pd.DataFrame(plan)


def build_receivable_events(ctx: GenContext, rec: pd.DataFrame, actuals: pd.DataFrame, plan: pd.DataFrame):
    """Replay each receivable's history into append-only status events; status = last event."""
    s = ctx.settings
    end = s.end
    pays = actuals[actuals["source_type"] == "RECEIVABLE"].sort_values(["recorded_at", "actual_id"])
    by_src = {k: g for k, g in pays.groupby("source_id")}
    wo = plan.set_index("receivable_id")["writeoff_date"]
    events = []
    status = {}
    for r in rec.itertuples(index=False):
        ev = [(r.invoice_date, r.invoice_date + pd.Timedelta(hours=9), "OPEN", "Invoice issued", None)]
        cur, paid = "OPEN", 0
        g = by_src.get(r.receivable_id)
        overdue_day = r.due_date + pd.Timedelta(days=1)
        overdue_ts = overdue_day + pd.Timedelta(hours=6)
        first_pay_ts = g["recorded_at"].iloc[0] if g is not None else None
        timeline = []
        if overdue_day <= end and (first_pay_ts is None or first_pay_ts > overdue_ts):
            timeline.append((overdue_ts, "OVERDUE_CHECK", None))
        if g is not None:
            timeline += [(t, "PAY", (aid, amt, d)) for t, aid, amt, d in
                         zip(g["recorded_at"], g["actual_id"], g["amount_minor"], g["actual_date"])]
        w = wo.get(r.receivable_id)
        if w is not None and not pd.isna(w):
            timeline.append((w + pd.Timedelta(hours=12), "WRITE_OFF", None))
        for ts, kind, data in sorted(timeline, key=lambda x: x[0]):
            if kind == "OVERDUE_CHECK" and paid == 0:
                ev.append((overdue_day, ts, "OVERDUE", "Unpaid after due date", None))
                cur = "OVERDUE"
            elif kind == "PAY":
                aid, amt, d = data
                paid += int(amt)
                new = "PAID" if paid >= r.amount_minor else "PARTIALLY_PAID"
                if new != cur:
                    ev.append((d, ts, new, "Payment received", aid))
                    cur = new
            elif kind == "WRITE_OFF" and cur == "OVERDUE":
                ev.append((ts.normalize(), ts, "WRITTEN_OFF", "Written off after collection period", None))
                cur = "WRITTEN_OFF"
        prev = None
        for d, ts, to, reason, aid in ev:
            events.append((r.receivable_id, prev, to, d, ts, reason, aid))
            prev = to
        status[r.receivable_id] = cur
    out = pd.DataFrame(events, columns=["receivable_id", "from_status", "to_status", "event_date", "recorded_at", "reason", "related_actual_id"])
    out = out.sort_values(["recorded_at", "receivable_id"], kind="mergesort").reset_index(drop=True)
    out.insert(0, "event_id", ids.seq_ids("RSE", s.id_year, len(out)))
    rec = rec.copy()
    rec["status"] = rec["receivable_id"].map(status)
    return out, rec
