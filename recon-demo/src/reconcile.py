"""Reconciliation engine: normalise -> dedupe -> match -> bucket -> bridge.

Every platform row and every bank line lands in exactly one bucket, so the bridge is algebra,
not luck:  Bank Oct total = Platform Oct total - In transit + Carry-in + Rounding + Duplicates
                            + (sum of unexplained items).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd

from . import config as C
from .calendar_utils import add_business_days, business_days_between, period_of, to_ist_date
from .money import ZERO, D, dsum, fmt

VALID_TYPES = {"PAYMENT", "REFUND"}
VALID_STATUS = {"SUCCESS", "FAILED"}


# ------------------------------------------------------------------ normalise
def normalise_platform(df: pd.DataFrame) -> pd.DataFrame:
    p = df.copy().reset_index(drop=True)
    if set(p["txn_type"]) - VALID_TYPES or set(p["status"]) - VALID_STATUS:
        raise ValueError("unknown txn_type or status in platform data")
    p["amount"] = p["amount"].map(D)
    p["created_ist"] = p["created_at_utc"].map(to_ist_date)
    p["signed"] = [a if t == "PAYMENT" else -a for a, t in zip(p["amount"], p["txn_type"])]
    p["original_txn_id"] = p["original_txn_id"].fillna("").astype(str).str.strip()
    return p


def normalise_bank(df: pd.DataFrame) -> pd.DataFrame:
    b = df.copy().reset_index(drop=True)
    b["settled_amount"] = b["settled_amount"].map(D)
    b["settlement_date"] = b["settlement_date"].map(lambda v: v if isinstance(v, date) else date.fromisoformat(str(v)))
    return b


# ------------------------------------------------------------------ result types
@dataclass
class Bridge:
    platform_total: Decimal
    in_transit: Decimal          # platform (full-precision) amount of Oct sales that settled in Nov
    carry_in: Decimal            # bank amount of pre-Oct sales that settled in Oct
    rounding: Decimal            # net (bank - platform) on matched Oct lines
    duplicates: Decimal          # duplicate bank lines settled in Oct
    bank_total: Decimal
    unexplained_items: dict      # category -> signed effect on the bridge
    counts: dict
    rounding_stats: dict

    @property
    def expected_bank(self) -> Decimal:
        return self.platform_total - self.in_transit + self.carry_in + self.rounding + self.duplicates

    @property
    def unexplained(self) -> Decimal:
        return self.bank_total - self.expected_bank

    @property
    def unclassified(self) -> Decimal:
        """Residual not accounted for by any named exception. Non-zero means a logic gap."""
        return self.unexplained - dsum(self.unexplained_items.values())

    def as_frame(self) -> pd.DataFrame:
        rows = [("Platform total (SUCCESS, created in Oct IST)", self.platform_total),
                ("- In transit (created Oct, settled Nov)", -self.in_transit),
                ("+ Carry-in (created before Oct, settled Oct)", self.carry_in),
                ("+ Rounding differences (net, matched lines)", self.rounding),
                ("+ Duplicate bank lines settled in Oct", self.duplicates),
                ("= Expected bank total", self.expected_bank),
                ("Bank total (lines settled in Oct)", self.bank_total),
                ("Unexplained residual", self.unexplained)]
        rows += [(f"    of which {k}", v) for k, v in self.unexplained_items.items()]
        return pd.DataFrame({"Step": [r[0] for r in rows], "Amount (INR)": [fmt(r[1], 3) for r in rows]})


@dataclass
class ReconResult:
    platform: pd.DataFrame
    bank: pd.DataFrame
    exceptions: pd.DataFrame
    bridge: Bridge


# ------------------------------------------------------------------ engine
def reconcile(platform_raw: pd.DataFrame, lines_raw: pd.DataFrame, batches_raw: pd.DataFrame | None = None) -> ReconResult:
    plat_all, bank_all = normalise_platform(platform_raw), normalise_bank(lines_raw)
    exc: list = []

    def flag(category, severity, ref, amount, effect, detail, line=""):
        exc.append(dict(category=category, severity=severity, ref=ref, amount=amount,
                        bridge_effect=effect, bridge_line=line, detail=detail))

    # 1. platform duplicates: keep the first, flag the rest
    dup_mask = plat_all.duplicated("txn_id", keep="first")
    for r in plat_all[dup_mask].itertuples():
        flag("DUPLICATE_PLATFORM_TXN", "ERROR", r.txn_id, r.signed, ZERO,
             "txn_id appears more than once on the platform; extra copy excluded from totals")
    plat = plat_all[~dup_mask].copy()
    plat_idx = {r.txn_id: r for r in plat.itertuples()}

    # 2. bank lines outside the coverage window cannot be used as evidence
    oor = bank_all["settlement_date"] < C.BANK_START
    for r in bank_all[oor].itertuples():
        flag("OUT_OF_RANGE_BANK_LINE", "ERROR", r.line_ref, r.settled_amount, ZERO,
             f"settlement date {r.settlement_date} is before the bank file window; excluded")
    bank = bank_all[~oor].sort_values(["settlement_date", "line_ref"], kind="stable").reset_index(drop=True)

    # 3. de-duplicate bank lines by txn_ref: earliest is primary
    bank["dup_rank"] = bank.groupby("txn_ref").cumcount()
    primary_line = {}
    for r in bank.itertuples():
        primary_line.setdefault(r.txn_ref, r.line_ref)

    bank_info, plat_info = [], {}
    matched_diffs = []
    for b in bank.itertuples():
        sp = period_of(b.settlement_date)
        info = dict(bucket="", effect=ZERO, diff=None, platform_amount=None, created_ist=None)
        if b.dup_rank > 0:
            info.update(bucket="DUPLICATE", effect=b.settled_amount if sp == "IN" else ZERO)
            flag("DUPLICATE_BANK_LINE", "ERROR", b.line_ref, b.settled_amount, info["effect"],
                 f"txn_ref {b.txn_ref} already settled on line {primary_line[b.txn_ref]}", "Duplicates")
        else:
            p = plat_idx.get(b.txn_ref)
            if p is None:
                eff = b.settled_amount if sp == "IN" else ZERO
                info.update(bucket="BANK_ONLY", effect=eff)
                flag("BANK_ONLY", "ERROR", b.line_ref, b.settled_amount, eff,
                     f"bank line for {b.txn_ref} has no platform transaction", "Unexplained")
            elif p.status != "SUCCESS":
                eff = b.settled_amount if sp == "IN" else ZERO
                info.update(bucket="SETTLED_NOT_SUCCESS", effect=eff, created_ist=p.created_ist)
                plat_info[p.txn_id] = dict(bucket="SETTLED_NOT_SUCCESS", line_ref=b.line_ref, settlement_date=b.settlement_date,
                                           bank_amount=b.settled_amount, diff=None)
                flag("SETTLED_NOT_SUCCESS", "ERROR", b.line_ref, b.settled_amount, eff,
                     f"bank settled {b.txn_ref}, which is {p.status} on the platform", "Unexplained")
            else:
                diff = b.settled_amount - p.signed
                cp = period_of(p.created_ist)
                lag = business_days_between(p.created_ist, b.settlement_date)
                if lag > C.MAX_LAG_BD:
                    flag("LATE_SETTLEMENT", "WARN", b.line_ref, b.settled_amount, ZERO,
                         f"settled {lag} business days after {p.created_ist} (expected {C.MIN_LAG_BD}-{C.MAX_LAG_BD})")
                elif lag < C.MIN_LAG_BD:
                    flag("SETTLEMENT_TOO_EARLY", "WARN", b.line_ref, b.settled_amount, ZERO,
                         f"settled {lag} business days after {p.created_ist}")
                eff = ZERO
                if (cp, sp) == ("IN", "IN"):
                    if abs(diff) <= C.ROUNDING_TOLERANCE:
                        bucket = "MATCHED"
                        matched_diffs.append(diff)
                    else:
                        bucket, eff = "AMOUNT_MISMATCH", diff
                        flag("AMOUNT_MISMATCH", "ERROR", b.line_ref, b.settled_amount, eff,
                             f"bank {b.settled_amount} vs platform {p.signed} (diff {diff})", "Unexplained")
                else:
                    bucket = {("IN", "POST"): "IN_TRANSIT", ("PRE", "IN"): "CARRY_IN"}.get((cp, sp), "OUT_OF_SCOPE")
                    if (cp, sp) == ("POST", "IN"):
                        bucket, eff = "SETTLED_BEFORE_CREATED", b.settled_amount
                        flag("SETTLED_BEFORE_CREATED", "ERROR", b.line_ref, b.settled_amount, eff,
                             f"settled in Oct but created {p.created_ist}", "Unexplained")
                    elif abs(diff) > C.ROUNDING_TOLERANCE:
                        flag("AMOUNT_MISMATCH", "WARN", b.line_ref, b.settled_amount, ZERO,
                             f"diff {diff} on a line outside the October bridge")
                info.update(bucket=bucket, effect=eff, diff=diff, platform_amount=p.signed, created_ist=p.created_ist)
                plat_info[p.txn_id] = dict(bucket=bucket, line_ref=b.line_ref, settlement_date=b.settlement_date,
                                           bank_amount=b.settled_amount, diff=diff)
        bank_info.append(info)

    # 4. platform rows with no primary bank line
    for p in plat.itertuples():
        if p.txn_id in plat_info:
            continue
        if p.status != "SUCCESS":
            plat_info[p.txn_id] = dict(bucket="NOT_EXPECTED_TO_SETTLE")
            continue
        cp = period_of(p.created_ist)
        if cp == "IN":
            bucket = "PLATFORM_ONLY"
            flag("PLATFORM_ONLY", "ERROR", p.txn_id, p.signed, -p.signed,
                 "SUCCESS in October but no bank settlement line", "Unexplained")
        elif cp == "PRE":
            if add_business_days(p.created_ist, C.MIN_LAG_BD) < C.BANK_START:
                bucket = "PRE_WINDOW"              # may have settled before the bank file starts
            else:
                bucket = "MISSING_SETTLEMENT"      # should be in the file, is not; no Oct bridge effect
                flag("MISSING_SETTLEMENT", "WARN", p.txn_id, p.signed, ZERO,
                     f"created {p.created_ist}, should have settled inside the bank window")
        else:
            bucket = "OUT_OF_SCOPE"
        plat_info[p.txn_id] = dict(bucket=bucket)

    # 5. refund integrity (searches the whole extract, any month or status)
    refund_total: dict = {}
    for r in plat[(plat.txn_type == "REFUND") & (plat.status == "SUCCESS")].itertuples():
        refund_total[r.original_txn_id] = refund_total.get(r.original_txn_id, ZERO) + r.amount
    for r in plat[plat.txn_type == "REFUND"].itertuples():
        oid, o = r.original_txn_id, plat_idx.get(r.original_txn_id)
        if not oid:
            flag("REFUND_MISSING_ORIGINAL_ID", "ERROR", r.txn_id, r.signed, ZERO, "refund carries no original_txn_id")
        elif o is None:
            flag("ORPHAN_REFUND", "ERROR", r.txn_id, r.signed, ZERO,
                 f"original '{oid}' not found in {len(plat)} platform rows; cash exposure {r.amount}")
        else:
            if o.txn_type != "PAYMENT" or o.status != "SUCCESS":
                flag("REFUND_INVALID_TARGET", "ERROR", r.txn_id, r.signed, ZERO,
                     f"original {oid} is a {o.status} {o.txn_type}, not a SUCCESS payment")
            elif refund_total.get(oid, ZERO) > o.amount:
                flag("REFUND_EXCEEDS_ORIGINAL", "ERROR", r.txn_id, r.signed, ZERO,
                     f"refunds total {refund_total[oid]} against an original of {o.amount}")
            if r.created_at_utc < o.created_at_utc:
                flag("REFUND_BEFORE_ORIGINAL", "ERROR", r.txn_id, r.signed, ZERO, f"refund precedes original {oid}")

    # 6. batch control totals
    if batches_raw is not None:
        calc = bank_all.groupby("batch_id")["settled_amount"].agg(lambda s: (len(s), dsum(s)))
        for r in batches_raw.itertuples():
            n, tot = calc.get(r.batch_id, (0, ZERO))
            if n != int(r.line_count) or tot != D(r.control_total):
                flag("BATCH_CONTROL_MISMATCH", "ERROR", r.batch_id, tot, ZERO,
                     f"control says {r.line_count} lines / {r.control_total}; lines give {n} / {tot}")

    # 7. assemble outputs + bridge
    info_cols = ["bucket", "effect", "diff", "platform_amount", "created_ist"]   # explicit so an empty bank file still works
    bank_out = pd.concat([bank, pd.DataFrame(bank_info, columns=info_cols)], axis=1)
    plat_out = plat.copy()
    for col in ("bucket", "line_ref", "settlement_date", "bank_amount", "diff"):
        plat_out[col] = [plat_info[t].get(col) for t in plat_out["txn_id"]]
    exc_df = pd.DataFrame(exc, columns=["category", "severity", "ref", "amount", "bridge_effect", "bridge_line", "detail"])

    in_oct = plat[(plat.status == "SUCCESS") & (plat.created_ist.map(period_of) == "IN")]
    in_transit_rows = bank_out[bank_out.bucket == "IN_TRANSIT"]
    carry_rows = bank_out[bank_out.bucket == "CARRY_IN"]
    dup_rows = bank_out[bank_out.bucket == "DUPLICATE"]
    unexplained = exc_df[exc_df.bridge_line == "Unexplained"]
    items: dict = {}
    for cat, eff in zip(unexplained.category, unexplained.bridge_effect):
        items[cat] = items.get(cat, ZERO) + eff
    bank_oct = dsum(bank_out.loc[bank_out.settlement_date.map(period_of) == "IN", "settled_amount"])
    bridge = Bridge(
        platform_total=dsum(in_oct["signed"]),
        in_transit=dsum(in_transit_rows["platform_amount"]),
        carry_in=dsum(carry_rows["settled_amount"]),
        rounding=dsum(matched_diffs),
        duplicates=dsum(dup_rows["effect"]),
        bank_total=bank_oct,
        unexplained_items=items,
        counts=dict(in_transit=len(in_transit_rows), carry_in=len(carry_rows), matched=len(matched_diffs),
                    duplicates=int((dup_rows["effect"] != 0).sum())),
        rounding_stats=dict(lines=len(matched_diffs), nonzero_lines=sum(1 for d in matched_diffs if d != 0),
                            max_abs=max((abs(d) for d in matched_diffs), default=ZERO)))
    return ReconResult(plat_out, bank_out, exc_df, bridge)
