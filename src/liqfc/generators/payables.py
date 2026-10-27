"""Payable generation (supplier, payroll, rent, tax, intercompany, one-off) and lifecycle events.

Rules (docs/business_rules.md, PAY-*):
* PAY-01 supplier scheduled_date = first payment-run weekday (Thu) on/after invoice + terms.
* PAY-02 approval = 2 business days before scheduled_date (not before invoice_date).
* PAY-03 suppliers: cancel with ``cancel_probability`` 1 business day before scheduled date;
  otherwise paid on scheduled date, or 1-2 business days late with ``late_payment_probability``.
* PAY-04 payroll paid on the 25th (prior business day); the forecast amount is the previous
  month's actual payroll (basis_actual_id); January uses the budget.
* PAY-05 rent: fixed, first business day of the month, known since 2025-12-01.
* PAY-06 tax: configured dates (next business day), known ``tax_notice_days`` before.
* PAY-07 one-off: large (0.5-2.5 x monthly revenue), known 20-45 days ahead, paid on schedule.
* PAY-08 intercompany payables mirror an intercompany receivable and settle on its due date.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from liqfc.core import ids
from liqfc.core.enums import PAYABLE_FLOW
from liqfc.generators.context import GenContext
from liqfc.generators.receivables import _accounts_for


def _months(s) -> list[pd.Period]:
    return list(pd.period_range(s.start, s.end, freq="M"))


def recurring_item_count(settings, n_entities: int) -> int:
    months = len(_months(settings))
    return n_entities * (2 * months + len(settings.gen["payable"]["tax_dates"]))


def generate_payables(ctx: GenContext, entities: pd.DataFrame, accounts: pd.DataFrame,
                      n_supplier: int, n_one_off: int, ic_pay: list[dict]):
    s = ctx.settings
    cfg = s.gen["payable"]
    cal = ctx.cal
    rng = ctx.rng("payables")
    cal_cfg = s.raw["calendar"]
    window = cal.business_days(cal_cfg["invoice_window_start"], cal_cfg["invoice_window_end"])
    window_months = len(pd.date_range(cal_cfg["invoice_window_start"], cal_cfg["invoice_window_end"])) / 30.4
    acc_ccy = accounts.set_index("account_id")["currency"].to_dict()
    eids = entities["entity_id"].to_numpy()
    size = np.array([ctx.entity_size[e] for e in eids], dtype=float)
    rows = []

    def op_account(e):
        return accounts[(accounts["entity_id"] == e) & (accounts["account_type"] == "OPERATING")]["account_id"].iloc[0]

    ent = rng.choice(eids, size=n_supplier, p=size / size.sum())
    acct = np.empty(n_supplier, dtype=object)
    for e in np.unique(ent):
        idx = np.where(ent == e)[0]
        cands, w = _accounts_for(accounts, e, cfg["account_type_weights"])
        acct[idx] = rng.choice(cands, size=len(idx), p=w)
    ccy = np.array([acc_ccy[a] for a in acct], dtype=object)
    invoice = pd.to_datetime(window[rng.integers(0, len(window), n_supplier)])
    terms = rng.choice(cfg["supplier_terms_days"], size=n_supplier, p=cfg["supplier_terms_weights"])
    sched = pd.to_datetime(cal.next_weekday_on_or_after(invoice + pd.to_timedelta(terms, unit="D"), cfg["payment_run_weekday"]))
    per_entity = pd.Series(ent).value_counts()
    mean_usd = np.array([ctx.entity_size[e] * cfg["supplier_spend_ratio"] * window_months / per_entity[e] for e in ent])
    sig = float(cfg["amount_sigma"])
    usd = np.exp(rng.normal(np.log(mean_usd) - sig**2 / 2, sig))
    amount = np.maximum(ctx.usd_to_local_minor(usd, ccy, invoice), 100)
    n_suppliers = max(20, n_supplier // 10)
    supplier = [f"SUPP-{int(x):05d}" for x in rng.integers(1, n_suppliers + 1, n_supplier)]
    u_cancel, u_late, late_days = rng.random(n_supplier), rng.random(n_supplier), rng.integers(1, 3, n_supplier)
    for i in range(n_supplier):
        cancel = u_cancel[i] < cfg["cancel_probability"]
        paid = pd.Timestamp(sched[i])
        if not cancel and u_late[i] < cfg["late_payment_probability"]:
            paid = pd.Timestamp(cal.add_business_days([sched[i]], int(late_days[i]))[0])
        rows.append(dict(entity_id=ent[i], account_id=acct[i], supplier_id=supplier[i], counterparty_entity_id=None,
                         ic_receivable_id=None, category="SUPPLIER", description="Supplier invoice",
                         invoice_date=invoice[i], scheduled_date=pd.Timestamp(sched[i]), currency=ccy[i],
                         amount_minor=int(amount[i]), x_actual_amount=int(amount[i]),
                         x_paid_date=pd.NaT if cancel else paid, x_cancel=cancel, x_counterparty=supplier[i]))

    rng_r = ctx.rng("payables:recurring")
    for e in eids:
        S = ctx.entity_size[e]
        payroll_acc = accounts[(accounts["entity_id"] == e) & (accounts["account_type"] == "PAYROLL")]["account_id"]
        p_acc = payroll_acc.iloc[0] if len(payroll_acc) else op_account(e)
        o_acc = op_account(e)
        prev_actual = None
        for m in _months(s):
            first_bd = pd.Timestamp(cal.roll_forward([m.start_time])[0])
            pay_day = pd.Timestamp(cal.roll_backward([pd.Timestamp(m.year, m.month, cfg["payroll_day"])])[0])
            true_usd = S * cfg["payroll_ratio"] * (1 + rng_r.normal(0, cfg["payroll_noise_sigma"]))
            true_amt = int(ctx.usd_to_local_minor([true_usd], [acc_ccy[p_acc]], [first_bd])[0])
            budget = int(ctx.usd_to_local_minor([S * cfg["payroll_ratio"]], [acc_ccy[p_acc]], [first_bd])[0])
            estimate = budget if prev_actual is None else prev_actual
            rows.append(dict(entity_id=e, account_id=p_acc, supplier_id=None, counterparty_entity_id=None,
                             ic_receivable_id=None, category="PAYROLL", description=f"Payroll {m}",
                             invoice_date=first_bd, scheduled_date=pay_day, currency=acc_ccy[p_acc],
                             amount_minor=estimate, x_actual_amount=true_amt, x_paid_date=pay_day, x_cancel=False,
                             x_counterparty="EMPLOYEES", x_payroll_month=str(m)))
            prev_actual = true_amt
            rent = int(ctx.usd_to_local_minor([S * cfg["rent_ratio"]], [acc_ccy[o_acc]], [pd.Timestamp("2025-12-01")])[0])
            rows.append(dict(entity_id=e, account_id=o_acc, supplier_id=None, counterparty_entity_id=None,
                             ic_receivable_id=None, category="RENT", description=f"Office rent {m}",
                             invoice_date=pd.Timestamp("2025-12-01"), scheduled_date=first_bd, currency=acc_ccy[o_acc],
                             amount_minor=rent, x_actual_amount=rent, x_paid_date=first_bd, x_cancel=False,
                             x_counterparty="LANDLORD"))
        for td in cfg["tax_dates"]:
            due = pd.Timestamp(cal.roll_forward([pd.Timestamp(td)])[0])
            known = due - pd.Timedelta(days=int(cfg["tax_notice_days"]))
            tax_usd = S * cfg["tax_ratio"] * 3 * (1 + rng_r.normal(0, 0.05))
            amt = int(ctx.usd_to_local_minor([tax_usd], [acc_ccy[o_acc]], [known])[0])
            rows.append(dict(entity_id=e, account_id=o_acc, supplier_id=None, counterparty_entity_id=None,
                             ic_receivable_id=None, category="TAX", description=f"Estimated tax {td}",
                             invoice_date=known, scheduled_date=due, currency=acc_ccy[o_acc], amount_minor=amt,
                             x_actual_amount=amt, x_paid_date=due, x_cancel=False, x_counterparty="TAX_AUTHORITY"))

    rng_o = ctx.rng("payables:one_off")
    pay_days = cal.business_days("2026-01-20", "2026-06-25")
    lo, hi = cfg["one_off_ratio_range"]
    nlo, nhi = cfg["one_off_notice_days_range"]
    for _ in range(n_one_off):
        e = str(rng_o.choice(eids, p=size / size.sum()))
        a = op_account(e)
        due = pd.Timestamp(pay_days[int(rng_o.integers(0, len(pay_days)))])
        known = due - pd.Timedelta(days=int(rng_o.integers(nlo, nhi + 1)))
        cat = str(rng_o.choice(cfg["one_off_categories"]))
        amt = int(ctx.usd_to_local_minor([ctx.entity_size[e] * rng_o.uniform(lo, hi)], [acc_ccy[a]], [known])[0])
        rows.append(dict(entity_id=e, account_id=a, supplier_id=None, counterparty_entity_id=None, ic_receivable_id=None,
                         category="ONE_OFF", description=f"One-off {cat.lower().replace('_', ' ')}",
                         invoice_date=known, scheduled_date=due, currency=acc_ccy[a], amount_minor=amt,
                         x_actual_amount=amt, x_paid_date=due, x_cancel=False, x_counterparty=cat))

    for p in ic_pay:
        rows.append(dict(entity_id=p["entity_id"], account_id=p["account_id"], supplier_id=None,
                         counterparty_entity_id=p["counterparty_entity_id"], ic_receivable_id=p["ic_receivable_id"],
                         category="INTERCOMPANY", description=f"Intercompany settlement {p['ic_receivable_id']}",
                         invoice_date=p["invoice_date"], scheduled_date=p["scheduled_date"], currency=p["currency"],
                         amount_minor=p["amount_minor"], x_actual_amount=p["amount_minor"],
                         x_paid_date=p["scheduled_date"], x_cancel=False, x_counterparty=p["counterparty_entity_id"]))

    pay = pd.DataFrame(rows)
    if "x_payroll_month" not in pay:
        pay["x_payroll_month"] = None
    pay["x_draw"] = np.arange(len(pay))
    pay = pay.sort_values(["invoice_date", "entity_id", "category", "scheduled_date", "amount_minor", "x_draw"],
                          kind="mergesort").reset_index(drop=True)
    pay.insert(0, "payable_id", ids.seq_ids("PAY", s.id_year, len(pay)))
    lead = int(cfg["approval_lead_business_days"])
    approve = pd.to_datetime(cal.add_business_days(pay["scheduled_date"], -lead))
    pay["x_approve_date"] = np.maximum(approve, pay["invoice_date"])
    pay["x_cancel_date"] = pd.to_datetime(np.where(pay["x_cancel"], pd.to_datetime(cal.add_business_days(pay["scheduled_date"], -1)), pd.NaT))
    pay["x_cancel_date"] = np.where(pay["x_cancel"], np.maximum(pay["x_cancel_date"], pay["invoice_date"]), pd.NaT)
    pay["x_cancel_date"] = pd.to_datetime(pay["x_cancel_date"])

    items = pd.DataFrame({
        "source_type": "PAYABLE", "source_id": pay["payable_id"], "entity_id": pay["entity_id"],
        "account_id": pay["account_id"], "currency": pay["currency"], "instalment_no": 1,
        "amount_minor": -pay["amount_minor"].astype(np.int64), "expected_date": pay["scheduled_date"],
        "known_date": pay["invoice_date"], "flow_category": pay["category"].map(PAYABLE_FLOW), "allocation_bp": 10000,
    })
    paid = pay[pay["x_paid_date"].notna() & (pay["x_paid_date"] <= s.end) & (pay["x_paid_date"] >= s.start)]
    settlements = pd.DataFrame({
        "source_type": "PAYABLE", "source_id": paid["payable_id"], "instalment_no": 1,
        "account_id": paid["account_id"], "currency": paid["currency"],
        "amount_minor": -paid["x_actual_amount"].astype(np.int64), "value_date": paid["x_paid_date"],
        "entry_type": paid["category"].map(PAYABLE_FLOW), "counterparty_ref": paid["x_counterparty"],
    })
    return pay, items, settlements


def build_payable_events(ctx: GenContext, pay: pd.DataFrame, actuals: pd.DataFrame):
    s = ctx.settings
    end = s.end
    act = actuals[actuals["source_type"] == "PAYABLE"].set_index("source_id")
    events, status = [], {}
    for r in pay.itertuples(index=False):
        ev = [(None, "SCHEDULED", r.invoice_date, r.invoice_date + pd.Timedelta(hours=9), "Obligation recorded", None)]
        cur = "SCHEDULED"
        cancel_before_approve = r.x_cancel and r.x_cancel_date <= r.x_approve_date
        if r.x_approve_date <= end and not cancel_before_approve:
            ev.append((cur, "APPROVED", r.x_approve_date, r.x_approve_date + pd.Timedelta(hours=10), "Approved for payment", None))
            cur = "APPROVED"
        if r.x_cancel and r.x_cancel_date <= end:
            ev.append((cur, "CANCELLED", r.x_cancel_date, r.x_cancel_date + pd.Timedelta(hours=11), "Cancelled before payment", None))
            cur = "CANCELLED"
        elif r.payable_id in act.index:
            a = act.loc[r.payable_id]
            ev.append((cur, "PAID", a["actual_date"], a["recorded_at"], "Payment executed", a["actual_id"]))
            cur = "PAID"
        for e in ev:
            events.append((r.payable_id, *e))
        status[r.payable_id] = cur
    out = pd.DataFrame(events, columns=["payable_id", "from_status", "to_status", "event_date", "recorded_at", "reason", "related_actual_id"])
    out = out.sort_values(["recorded_at", "payable_id"], kind="mergesort").reset_index(drop=True)
    out.insert(0, "event_id", ids.seq_ids("PSE", s.id_year, len(out)))
    pay = pay.copy()
    pay["status"] = pay["payable_id"].map(status)
    return out, pay
