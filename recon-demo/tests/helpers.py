"""Tiny builders for hand-made edge-case datasets. Calendar cheat-sheet (2026):
Sep 25 Fri | Sep 28 Mon | Sep 29 Tue | Sep 30 Wed | Oct 1 Thu | Oct 14 Wed | Oct 15 Thu | Oct 19 Mon
Oct 30 Fri | Oct 31 Sat | Nov 1 Sun | Nov 2 Mon | Nov 3 Tue"""
import pandas as pd

from src.reconcile import reconcile

P_COLS = ["txn_id", "created_at_utc", "txn_type", "status", "amount", "currency", "original_txn_id", "merchant_id"]
B_COLS = ["batch_id", "line_ref", "txn_ref", "settlement_date", "settled_amount", "currency"]


def ts(day: str) -> str:
    """Midday IST on the given day, expressed in UTC (06:30Z), so the IST date equals `day`."""
    return f"{day}T06:30:00Z"


def platform(*rows) -> pd.DataFrame:
    """rows: (txn_id, created_utc, type, status, amount[, original_txn_id])"""
    return pd.DataFrame([dict(txn_id=r[0], created_at_utc=r[1], txn_type=r[2], status=r[3], amount=r[4],
                              currency="INR", original_txn_id=r[5] if len(r) > 5 else "", merchant_id="M-1")
                         for r in rows], columns=P_COLS)


def bank(*rows) -> pd.DataFrame:
    """rows: (line_ref, txn_ref, 'YYYY-MM-DD', signed_amount)"""
    return pd.DataFrame([dict(batch_id="BATCH-" + r[2].replace("-", ""), line_ref=r[0], txn_ref=r[1],
                              settlement_date=r[2], settled_amount=r[3], currency="INR") for r in rows], columns=B_COLS)


def batches(*rows) -> pd.DataFrame:
    """rows: (batch_id, 'YYYY-MM-DD', line_count, control_total)"""
    return pd.DataFrame([dict(batch_id=r[0], settlement_date=r[1], line_count=r[2], control_total=r[3]) for r in rows])


def run(p, b, bt=None):
    return reconcile(p, b, bt)


def cats(res) -> list:
    return sorted(res.exceptions.category.tolist())
