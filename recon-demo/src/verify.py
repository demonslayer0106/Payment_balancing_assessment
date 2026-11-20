"""Compare what the reconciler found with the generator's ground-truth manifest (the answer key)."""
from __future__ import annotations

import pandas as pd

from .money import D


def verify_against_manifest(res, manifest: dict) -> pd.DataFrame:
    rows: list = []

    def add(name, expected, actual):
        rows.append(dict(check=name, expected=str(expected), actual=str(actual),
                         status="PASS" if expected == actual else "FAIL"))

    exc, bridge, ex, pl = res.exceptions, res.bridge, manifest["expected"], manifest["planted"]
    err = exc[exc.severity == "ERROR"]
    refs = lambda cat: set(exc.loc[exc.category == cat, "ref"])

    dup = pl.get("duplicate")
    orphan = pl.get("orphan_refund")
    add("Duplicate bank line found", {dup["duplicate_line_ref"]} if dup else set(), refs("DUPLICATE_BANK_LINE"))
    add("Orphan refund found", {orphan["refund_txn_id"]} if orphan else set(), refs("ORPHAN_REFUND"))
    add("No other errors (zero false positives)",
        ({"DUPLICATE_BANK_LINE", "ORPHAN_REFUND"} if dup else set()), set(err.category))
    add("No unexpected warnings", 0, int((exc.severity == "WARN").sum()))

    bt = pl.get("boundary_txn")
    if bt:
        bucket = res.platform.set_index("txn_id").loc[bt["txn_id"], "bucket"]
        add("Boundary txn (31 Oct UTC / 1 Nov IST) treated as November", "OUT_OF_SCOPE", bucket)

    decoys = set(manifest["decoys"]["refunds_of_september_sales"]) | set(manifest["decoys"]["refunds_of_late_october_sales"])
    decoys |= {t for g in manifest["decoys"]["same_amount_same_day_payments"] for t in g}
    add("Decoys not flagged", set(), decoys & set(exc.ref))

    add("Platform October total", D(ex["platform_oct_total"]), bridge.platform_total)
    add("In-transit amount", D(ex["in_transit_amount"]), bridge.in_transit)
    add("In-transit count", ex["in_transit_count"], bridge.counts["in_transit"])
    add("Carry-in amount", D(ex["carry_in_amount"]), bridge.carry_in)
    add("Carry-in count", ex["carry_in_count"], bridge.counts["carry_in"])
    add("Rounding net", D(ex["rounding_net"]), bridge.rounding)
    add("Duplicates amount", D(ex["duplicates_amount"]), bridge.duplicates)
    add("Bank October total", D(ex["bank_oct_total"]), bridge.bank_total)
    add("Unexplained residual", D("0"), bridge.unexplained)
    add("Fully classified (no logic gap)", D("0"), bridge.unclassified)
    return pd.DataFrame(rows)
