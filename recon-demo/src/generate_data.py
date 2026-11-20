"""Synthetic platform + bank data, with the four discrepancies planted among realistic decoys.

Design rules (see README):
  * Everything is derived from one seed -> reproducible.
  * Base data is generated first and is fully reconcilable; defects are planted afterwards
    (``plant_defects=False`` gives the clean baseline).
  * A ground-truth manifest records what was planted. The reconciler never reads it.
  * Floats are used only for sampling; every amount is converted to Decimal immediately.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .calendar_utils import add_business_days, period_of
from .money import D, dsum, round_half_up

N_PAYMENTS = 1400
FAIL_RATE = 0.035
N_REFUNDS = 65
N_REFUNDS_OF_SEPT = 10          # legitimate October refunds of September sales (decoys)
N_REFUNDS_OF_LATE_OCT = 2       # legitimate refunds of sales made Oct 29 that settle late (decoys)
PRICE_POINTS = [99, 149, 199, 299, 499, 999, 1499, 2499]
HOUR_WEIGHTS = [.2, .1, .1, .1, .1, .2, .5, 1, 2, 3, 4, 5, 5, 4.5, 4, 4, 4.5, 5, 5.5, 6, 5, 4, 2.5, 1]
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
BOUNDARY_TS = datetime(2026, 10, 31, 19, 0, 0, tzinfo=C.UTC)    # = 00:30 IST on 1 Nov


@dataclass
class Generated:
    platform: pd.DataFrame
    bank_lines: pd.DataFrame
    bank_batches: pd.DataFrame
    manifest: dict


# ---------------------------------------------------------------- helpers
def _new_id(rng: random.Random, used: set, prefix: str = "TXN") -> str:
    while True:
        i = f"{prefix}-{''.join(rng.choice(ALPHABET) for _ in range(8))}"
        if i not in used:
            used.add(i)
            return i


def _ist_date(r: dict) -> date:
    return r["created"].astimezone(C.IST).date()


def _signed(r: dict) -> Decimal:
    return r["amount"] if r["txn_type"] == "PAYMENT" else -r["amount"]


def _bank_amount(r: dict) -> Decimal:
    return round_half_up(r["amount"]) if r["txn_type"] == "PAYMENT" else -r["amount"]


def _day_weights(days):
    out = []
    for d in days:
        w = 1.0 if d.weekday() < 5 else (0.55 if d.weekday() == 5 else 0.45)
        if d.day <= 2 or d.day >= 29:       # start/end-of-month bump
            w *= 1.25
        out.append(w)
    return out


def _amount(rng: random.Random, nprng) -> Decimal:
    if rng.random() < 0.35:                                   # subscription-style price points
        return Decimal(rng.choice(PRICE_POINTS))
    cents = int(min(max(nprng.lognormal(np.log(900), 0.9), 20), 150000) * 100)
    amt = Decimal(cents) / 100                                # right-skewed ticket sizes
    if rng.random() < 0.20:                                   # tax/percentage-derived: sub-paisa precision
        digit = 5 if rng.random() < 0.40 else rng.choice([1, 2, 3, 4, 6, 7, 8, 9])
        amt += Decimal(digit) / 1000                          # exact half-paisas are over-represented
    return amt


def _ist_dt(d: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=C.IST).astimezone(C.UTC)


def _refund_row(rng, used, orig, created):
    cents = int((orig["amount"] * 100).to_integral_value(rounding=ROUND_DOWN))
    if rng.random() >= 0.7:                                   # 30% partial refunds
        cents = max(100, int(cents * rng.uniform(0.3, 0.9)))
    return dict(txn_id=_new_id(rng, used), created=created, txn_type="REFUND", status="SUCCESS",
                amount=Decimal(cents) / 100, original=orig["txn_id"], merchant=orig["merchant"], decoy=None)


def _refund_time(rng, orig_created):
    created = orig_created + timedelta(days=rng.randint(1, 9), hours=rng.randint(0, 5),
                                       minutes=rng.randint(0, 59))
    if created.astimezone(C.IST).date() > C.PERIOD_END:
        created = _ist_dt(C.PERIOD_END, rng.randint(9, 20), rng.randrange(60))
    return created


def _corrupt_id(prng, txn_id, used):
    prefix, suffix = txn_id.split("-")
    while True:
        i = prng.randrange(len(suffix))
        c = prng.choice(ALPHABET.replace(suffix[i], ""))
        new = f"{prefix}-{suffix[:i]}{c}{suffix[i + 1:]}"
        if new not in used:
            return new


# ---------------------------------------------------------------- generator
def generate(seed: int = C.SEED, plant_defects: bool = True) -> Generated:
    rng, nprng, prng = random.Random(seed), np.random.default_rng(seed), random.Random(seed + 1)
    used: set = set()
    days = [C.LOOKBACK_START + timedelta(days=i) for i in range((C.PERIOD_END - C.LOOKBACK_START).days + 1)]
    weights = _day_weights(days)

    # 1. payments -------------------------------------------------------
    rows: list = []
    for _ in range(N_PAYMENTS):
        d = rng.choices(days, weights)[0]
        h = rng.choices(range(24), HOUR_WEIGHTS)[0]
        rows.append(dict(txn_id=_new_id(rng, used), created=_ist_dt(d, h, rng.randrange(60), rng.randrange(60)),
                         txn_type="PAYMENT", status="FAILED" if rng.random() < FAIL_RATE else "SUCCESS",
                         amount=_amount(rng, nprng), original=None,
                         merchant=f"M-{rng.randint(1001, 1040)}", decoy=None))

    # 2. refunds (all legitimate for now) ---------------------------------
    paid = [r for r in rows if r["status"] == "SUCCESS"]
    sept = [r for r in paid if period_of(_ist_date(r)) == "PRE"]
    late = [r for r in paid if _ist_date(r) == date(2026, 10, 29)]
    mid = [r for r in paid if C.PERIOD_START <= _ist_date(r) <= date(2026, 10, 28)]
    n_plain = N_REFUNDS - N_REFUNDS_OF_SEPT - N_REFUNDS_OF_LATE_OCT
    for orig in rng.sample(sept, N_REFUNDS_OF_SEPT):
        t = _ist_dt(date(2026, 10, rng.randint(1, 28)), rng.randint(9, 20), rng.randrange(60))
        r = _refund_row(rng, used, orig, t)
        r["decoy"] = "refund_of_september_sale"
        rows.append(r)
    for orig in rng.sample(late, N_REFUNDS_OF_LATE_OCT):
        t = _ist_dt(C.PERIOD_END, rng.randint(10, 20), rng.randrange(60))
        r = _refund_row(rng, used, orig, t)
        r["decoy"] = "refund_of_late_october_sale"
        rows.append(r)
    for orig in rng.sample(mid, n_plain):
        rows.append(_refund_row(rng, used, orig, _refund_time(rng, orig["created"])))

    # 3. expected settlement dates (1st or 2nd business day after the IST date, 70/30)
    for r in rows:
        if r["status"] == "SUCCESS":
            r["settle"] = add_business_days(_ist_date(r), 1 if rng.random() < 0.7 else 2)

    planted = {"duplicate": None, "orphan_refund": None, "boundary_txn": None}
    dup_line = None

    # 4. plant the defects (separate RNG so the base data is identical with/without) --------
    if plant_defects:
        # timing trap: 19:00 UTC on 31 Oct is 00:30 IST on 1 Nov -> belongs to November
        b = dict(txn_id=_new_id(prng, used), created=BOUNDARY_TS, txn_type="PAYMENT", status="SUCCESS",
                 amount=Decimal("1299.000"), original=None, merchant="M-1007", decoy=None)
        b["settle"] = add_business_days(_ist_date(b), 1)
        rows.append(b)
        planted["boundary_txn"] = dict(txn_id=b["txn_id"], created_at_utc=BOUNDARY_TS.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       ist_date=str(_ist_date(b)), settle_date=str(b["settle"]))

        # orphan refund: well-formed ID that exists nowhere (one-character corruption of a real ID)
        cands = [r for r in rows if r["txn_type"] == "REFUND" and r["decoy"] is None
                 and date(2026, 10, 12) <= _ist_date(r) <= date(2026, 10, 20)
                 and Decimal(400) <= r["amount"] <= Decimal(2500)]
        o = prng.choice(cands)
        true_orig = o["original"]
        o["original"] = _corrupt_id(prng, true_orig, used)
        planted["orphan_refund"] = dict(refund_txn_id=o["txn_id"], original_as_written=o["original"],
                                        original_before_corruption=true_orig, amount=str(o["amount"]))

        # bank-side duplicate: same txn_ref paid again 1-2 business days later, new line_ref
        cands = [r for r in rows if r["txn_type"] == "PAYMENT" and r["status"] == "SUCCESS"
                 and date(2026, 10, 6) <= r["settle"] <= date(2026, 10, 20)
                 and Decimal(400) <= _bank_amount(r) <= Decimal(2500)]
        src = prng.choice(cands)
        dup_line = dict(txn_ref=src["txn_id"], settlement_date=add_business_days(src["settle"], prng.choice([1, 2])),
                        settled_amount=_bank_amount(src), is_dup=True)
        planted["duplicate"] = dict(txn_ref=src["txn_id"], primary_settle=str(src["settle"]),
                                    duplicate_settle=str(dup_line["settlement_date"]), amount=str(dup_line["settled_amount"]))

    # 5. bank lines ---------------------------------------------------------
    lines = [dict(txn_ref=r["txn_id"], settlement_date=r["settle"], settled_amount=_bank_amount(r), is_dup=False)
             for r in rows if r["status"] == "SUCCESS" and C.BANK_START <= r["settle"] <= C.BANK_END]
    if dup_line:
        lines.append(dup_line)
    lrng = random.Random(seed + 2)
    by_date: dict = {}
    for ln in lines:
        by_date.setdefault(ln["settlement_date"], []).append(ln)
    bank_rows, batch_rows = [], []
    for d in sorted(by_date):
        group = by_date[d]
        lrng.shuffle(group)
        for i, ln in enumerate(group, 1):
            ln["batch_id"], ln["line_ref"] = f"BATCH-{d:%Y%m%d}", f"LN-{d:%Y%m%d}-{i:04d}"
            bank_rows.append(ln)
        batch_rows.append(dict(batch_id=f"BATCH-{d:%Y%m%d}", settlement_date=str(d), line_count=len(group),
                               control_total=f"{dsum(x['settled_amount'] for x in group):.2f}"))
    if dup_line:
        planted["duplicate"]["duplicate_line_ref"] = dup_line["line_ref"]
        planted["duplicate"]["primary_line_ref"] = next(
            x["line_ref"] for x in bank_rows if x["txn_ref"] == dup_line["txn_ref"] and not x["is_dup"])

    # 6. frames ---------------------------------------------------------------
    srt = sorted(rows, key=lambda r: r["created"])
    platform = pd.DataFrame({
        "txn_id": [r["txn_id"] for r in srt],
        "created_at_utc": [r["created"].strftime("%Y-%m-%dT%H:%M:%SZ") for r in srt],
        "txn_type": [r["txn_type"] for r in srt],
        "status": [r["status"] for r in srt],
        "amount": [f"{r['amount']:.3f}" for r in srt],
        "currency": C.CURRENCY,
        "original_txn_id": [r["original"] or "" for r in srt],
        "merchant_id": [r["merchant"] for r in srt]})
    bank_lines = pd.DataFrame({
        "batch_id": [x["batch_id"] for x in bank_rows], "line_ref": [x["line_ref"] for x in bank_rows],
        "txn_ref": [x["txn_ref"] for x in bank_rows], "settlement_date": [str(x["settlement_date"]) for x in bank_rows],
        "settled_amount": [f"{x['settled_amount']:.2f}" for x in bank_rows], "currency": C.CURRENCY})
    batches = pd.DataFrame(batch_rows)

    # 7. ground truth -----------------------------------------------------------
    success = [r for r in rows if r["status"] == "SUCCESS"]
    oct_rows = [r for r in success if period_of(_ist_date(r)) == "IN"]
    in_transit = [r for r in oct_rows if period_of(r["settle"]) == "POST"]
    matched = [r for r in oct_rows if period_of(r["settle"]) == "IN"]
    carry = [r for r in success if period_of(_ist_date(r)) == "PRE" and period_of(r["settle"]) == "IN"]
    groups: dict = {}
    for r in rows:
        if r["txn_type"] == "PAYMENT" and r["status"] == "SUCCESS":
            groups.setdefault((str(r["amount"]), str(_ist_date(r))), []).append(r["txn_id"])
    same_amt = sorted((g for g in groups.values() if len(g) >= 2), key=len, reverse=True)[:8]
    manifest = {
        "seed": seed, "plant_defects": plant_defects,
        "period": {"start": str(C.PERIOD_START), "end": str(C.PERIOD_END), "lookback_start": str(C.LOOKBACK_START)},
        "planted": planted,
        "decoys": {
            "refunds_of_september_sales": [r["txn_id"] for r in rows if r["decoy"] == "refund_of_september_sale"],
            "refunds_of_late_october_sales": [r["txn_id"] for r in rows if r["decoy"] == "refund_of_late_october_sale"],
            "same_amount_same_day_payments": same_amt},
        "expected": {
            "platform_oct_total": str(dsum(_signed(r) for r in oct_rows)),
            "in_transit_amount": str(dsum(_signed(r) for r in in_transit)), "in_transit_count": len(in_transit),
            "carry_in_amount": str(dsum(_bank_amount(r) for r in carry)), "carry_in_count": len(carry),
            "rounding_net": str(dsum(_bank_amount(r) - _signed(r) for r in matched)),
            "duplicates_amount": str(dup_line["settled_amount"]) if dup_line else "0",
            "bank_oct_total": str(dsum(x["settled_amount"] for x in bank_rows if period_of(x["settlement_date"]) == "IN"))},
        "row_counts": {"platform": len(platform), "bank_lines": len(bank_lines), "batches": len(batches),
                       "refunds": int((platform.txn_type == "REFUND").sum()),
                       "failed": int((platform.status == "FAILED").sum())}}
    return Generated(platform, bank_lines, batches, manifest)


def write_files(gen: Generated, outdir) -> None:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    gen.platform.to_csv(out / "platform_transactions.csv", index=False)
    gen.bank_lines.to_csv(out / "bank_settlement_lines.csv", index=False)
    gen.bank_batches.to_csv(out / "bank_batches.csv", index=False)
    (out / "ground_truth.json").write_text(json.dumps(gen.manifest, indent=2, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate synthetic reconciliation data")
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=C.SEED)
    ap.add_argument("--clean", action="store_true", help="no planted defects")
    a = ap.parse_args()
    g = generate(a.seed, plant_defects=not a.clean)
    write_files(g, a.out)
    print(json.dumps(g.manifest["row_counts"]), "->", a.out)
