"""Generator constraints: proves the synthetic data is *reconcilable* before any defect hunting.

Independent of reconcile.py on purpose (different code path, same rules)."""
from __future__ import annotations

from datetime import date

from . import config as C
from .calendar_utils import business_days_between, is_business_day, parse_utc, period_of, to_ist_date
from .money import D, dsum, round_half_up


def validate(gen) -> list:
    p, l, b, m = gen.platform, gen.bank_lines, gen.bank_batches, gen.manifest
    passed: list = []

    def ok(cond, name):
        if not cond:
            raise AssertionError(f"Generator constraint failed: {name}")
        passed.append(name)

    planted = m["planted"]
    ids = set(p.txn_id)
    prow = p.set_index("txn_id")
    ok(p.txn_id.is_unique, "txn_id is unique (no platform duplicates)")
    ok(l.line_ref.is_unique, "line_ref is unique")
    ok(set(l.txn_ref) <= ids, "every bank txn_ref resolves to a platform row")
    ok((prow.loc[l.txn_ref.unique(), "status"] == "SUCCESS").all(), "no FAILED transaction appears in the bank file")
    ok(all(parse_utc(t) <= C.EXTRACT_END_UTC for t in p.created_at_utc), "platform extract ends 31 Oct 23:59:59 UTC")
    ok(all(D(a) > 0 for a in p.amount), "platform amounts are positive")
    ok(((p.txn_type == "PAYMENT") == (p.original_txn_id == "")).all(), "original_txn_id set for refunds only")

    # batch control totals
    for r in b.itertuples():
        g = l[l.batch_id == r.batch_id]
        ok(len(g) == int(r.line_count) and dsum(D(x) for x in g.settled_amount) == D(r.control_total),
           f"control total and line count match for {r.batch_id}")

    # dates and lags
    dates = l.settlement_date.map(date.fromisoformat)
    ok(all(is_business_day(d) for d in dates), "every settlement date is a weekday")
    ok(all(C.BANK_START <= d <= C.BANK_END for d in dates), "bank file covers 1 Oct to 3 Nov")
    first = l.assign(d=dates).sort_values(["d", "line_ref"]).drop_duplicates("txn_ref")
    lags = [business_days_between(to_ist_date(prow.loc[r.txn_ref, "created_at_utc"]), r.d) for r in first.itertuples()]
    ok(all(C.MIN_LAG_BD <= x <= C.MAX_LAG_BD for x in lags), "settlement lag is 1-2 business days after the IST date")

    # amounts: bank = round_half_up(platform) for payments, negative platform amount for refunds
    for r in l.itertuples():
        pr = prow.loc[r.txn_ref]
        want = round_half_up(pr.amount) if pr.txn_type == "PAYMENT" else -D(pr.amount)
        ok(D(r.settled_amount) == want, f"bank amount for {r.line_ref} follows half-up rounding / refund sign")

    # refunds
    orphan = (planted.get("orphan_refund") or {}).get("refund_txn_id")
    refunds = p[p.txn_type == "REFUND"]
    ok(refunds.status.eq("SUCCESS").all(), "all refunds are SUCCESS")
    legit = refunds[refunds.txn_id != orphan]
    ok(legit.original_txn_id.isin(ids).all(), "every legitimate refund's original exists in the extract")
    ok(legit.original_txn_id.is_unique, "at most one refund per original")
    ok(all(prow.loc[o, "txn_type"] == "PAYMENT" and prow.loc[o, "status"] == "SUCCESS" for o in legit.original_txn_id),
       "legitimate refunds point at SUCCESS payments")
    ok(all(D(r.amount) <= D(prow.loc[r.original_txn_id, "amount"]) for r in legit.itertuples()),
       "no refund exceeds its original")
    if orphan:
        ok(prow.loc[orphan, "original_txn_id"] not in ids, "only the planted orphan has an unresolvable original")

    # planted items
    if planted.get("duplicate"):
        d = planted["duplicate"]
        g = l[l.txn_ref == d["txn_ref"]]
        ok(len(g) == 2 and g.line_ref.is_unique, "duplicate: same txn_ref twice with different line_refs")
        ok(all(period_of(date.fromisoformat(x)) == "IN" for x in g.settlement_date), "duplicate: both copies settle in October")
    if planted.get("boundary_txn"):
        t = planted["boundary_txn"]
        ok(t["created_at_utc"].startswith("2026-10-31") and to_ist_date(t["created_at_utc"]) == date(2026, 11, 1),
           "boundary txn is 31 Oct in UTC but 1 Nov in IST")
        ok(period_of(date.fromisoformat(t["settle_date"])) == "POST", "boundary txn settles in November")
    return passed
