"""Hand-built edge cases. Each test names the business rule it protects."""
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.calendar_utils import add_business_days
from src.money import D, round_half_up
from tests.helpers import B_COLS, P_COLS, bank, batches, cats, platform, run, ts

PAY, REF, OK, BAD = "PAYMENT", "REFUND", "SUCCESS", "FAILED"


# ------------------------------------------------------------------ timing
def test_transaction_settling_next_month_is_in_transit_not_an_error():
    r = run(platform(("T1", ts("2026-10-30"), PAY, OK, "100.000")), bank(("L1", "T1", "2026-11-02", "100.00")))
    assert r.bank.bucket.tolist() == ["IN_TRANSIT"]
    assert r.bridge.platform_total == 100 and r.bridge.in_transit == 100 and r.bridge.bank_total == 0
    assert r.bridge.unexplained == 0 and r.exceptions.empty


def test_september_sale_settling_in_october_is_carry_in():
    r = run(platform(("S1", ts("2026-09-30"), PAY, OK, "50.000")), bank(("L1", "S1", "2026-10-01", "50.00")))
    assert r.bank.bucket.tolist() == ["CARRY_IN"]
    assert r.bridge.carry_in == 50 and r.bridge.platform_total == 0 and r.bridge.unexplained == 0


def test_ist_cutoff_not_utc_decides_the_month():
    p = platform(("A", "2026-10-31T18:29:59Z", PAY, OK, "10.000"),     # 23:59:59 IST on 31 Oct -> October
                 ("B", "2026-10-31T18:30:00Z", PAY, OK, "20.000"))     # 00:00:00 IST on 1 Nov -> November
    r = run(p, bank(("L1", "A", "2026-11-02", "10.00"), ("L2", "B", "2026-11-02", "20.00")))
    assert r.bridge.platform_total == 10                                # a UTC cutoff would give 30
    assert r.platform.set_index("txn_id").loc["B", "bucket"] == "OUT_OF_SCOPE"
    assert r.bridge.unexplained == 0


def test_pre_window_sale_without_bank_line_is_not_flagged_but_a_missing_one_is():
    p = platform(("S25", ts("2026-09-25"), PAY, OK, "10.000"),         # could settle before 1 Oct -> unknowable
                 ("S30", ts("2026-09-30"), PAY, OK, "20.000"))         # must settle 1-2 Oct -> expected in file
    r = run(p, bank())
    assert r.platform.set_index("txn_id").bucket.to_dict() == {"S25": "PRE_WINDOW", "S30": "MISSING_SETTLEMENT"}
    assert cats(r) == ["MISSING_SETTLEMENT"] and r.bridge.unexplained == 0   # warning only, no Oct bridge effect


# ------------------------------------------------------------------ late / early settlement
def test_late_settlement_is_flagged_but_still_bridged_by_period():
    r = run(platform(("T1", ts("2026-10-14"), PAY, OK, "10.000")), bank(("L1", "T1", "2026-10-19", "10.00")))
    assert cats(r) == ["LATE_SETTLEMENT"] and r.exceptions.severity.tolist() == ["WARN"]
    assert r.bank.bucket.tolist() == ["MATCHED"] and r.bridge.unexplained == 0


def test_very_late_cross_month_settlement_is_both_in_transit_and_late():
    r = run(platform(("T1", ts("2026-10-30"), PAY, OK, "10.000")), bank(("L1", "T1", "2026-11-05", "10.00")))
    assert r.bank.bucket.tolist() == ["IN_TRANSIT"] and cats(r) == ["LATE_SETTLEMENT"]


def test_same_day_settlement_is_suspiciously_early():
    r = run(platform(("T1", ts("2026-10-15"), PAY, OK, "10.000")), bank(("L1", "T1", "2026-10-15", "10.00")))
    assert cats(r) == ["SETTLEMENT_TOO_EARLY"]


# ------------------------------------------------------------------ rounding tolerance
def test_half_paisa_difference_is_tolerated_and_reported_as_rounding():
    r = run(platform(("T1", ts("2026-10-14"), PAY, OK, "100.005")), bank(("L1", "T1", "2026-10-15", "100.01")))
    assert r.exceptions.empty and r.bridge.rounding == Decimal("0.005") and r.bridge.unexplained == 0


def test_rounding_hidden_per_line_shows_up_in_the_total():
    n = 200
    p = platform(*[(f"T{i}", ts("2026-10-14"), PAY, OK, "10.005") for i in range(n)])
    b = bank(*[(f"L{i}", f"T{i}", "2026-10-15", "10.01") for i in range(n)])
    r = run(p, b)
    assert r.exceptions.empty                                           # no single line is an exception
    assert r.bridge.rounding == Decimal("1.000")                        # but the aggregate drift is 1.00
    assert r.bridge.rounding_stats["nonzero_lines"] == n and r.bridge.rounding_stats["max_abs"] == Decimal("0.005")
    assert r.bridge.unexplained == 0


@pytest.mark.parametrize("plat,bank_amt,diff", [("100.000", "100.01", "0.010"), ("10.004", "10.01", "0.006")])
def test_difference_beyond_half_paisa_is_an_amount_mismatch(plat, bank_amt, diff):
    r = run(platform(("T1", ts("2026-10-14"), PAY, OK, plat)), bank(("L1", "T1", "2026-10-15", bank_amt)))
    assert cats(r) == ["AMOUNT_MISMATCH"]
    assert r.bridge.unexplained == D(diff) and r.bridge.unclassified == 0


def test_refund_booked_with_wrong_sign_is_caught():
    p = platform(("P1", ts("2026-10-10"), PAY, OK, "100.000"), ("R1", ts("2026-10-14"), REF, OK, "25.000", "P1"))
    r = run(p, bank(("L1", "P1", "2026-10-12", "100.00"), ("L2", "R1", "2026-10-15", "25.00")))   # should be -25.00
    assert cats(r) == ["AMOUNT_MISMATCH"] and r.bridge.unexplained == 50


# ------------------------------------------------------------------ duplicates
def test_bank_duplicate_in_two_batches():
    p = platform(("T1", ts("2026-10-14"), PAY, OK, "200.000"))
    r = run(p, bank(("L1", "T1", "2026-10-15", "200.00"), ("L2", "T1", "2026-10-19", "200.00")))
    assert cats(r) == ["DUPLICATE_BANK_LINE"] and r.exceptions.ref.tolist() == ["L2"]
    assert r.bridge.duplicates == 200 and r.bridge.unexplained == 0
    assert r.bank.set_index("line_ref").loc["L1", "bucket"] == "MATCHED"


def test_bank_duplicate_inside_one_batch_keeps_earliest_line_ref_as_primary():
    p = platform(("T1", ts("2026-10-14"), PAY, OK, "200.000"))
    r = run(p, bank(("L9", "T1", "2026-10-15", "200.00"), ("L1", "T1", "2026-10-15", "200.00")))
    assert r.exceptions.ref.tolist() == ["L9"] and r.bridge.unexplained == 0


def test_duplicate_that_lands_in_november_is_flagged_but_not_in_the_october_bridge():
    p = platform(("T1", ts("2026-10-29"), PAY, OK, "200.000"))
    r = run(p, bank(("L1", "T1", "2026-10-30", "200.00"), ("L2", "T1", "2026-11-02", "200.00")))
    assert cats(r) == ["DUPLICATE_BANK_LINE"] and r.bridge.duplicates == 0 and r.bridge.unexplained == 0


def test_same_amount_same_day_payments_are_not_duplicates():
    p = platform(*[(f"T{i}", ts("2026-10-14"), PAY, OK, "499.000") for i in range(5)])
    r = run(p, bank(*[(f"L{i}", f"T{i}", "2026-10-15", "499.00") for i in range(5)]))
    assert r.exceptions.empty


def test_platform_duplicate_is_flagged_and_counted_once():
    row = ("T1", ts("2026-10-14"), PAY, OK, "10.000")
    r = run(platform(row, row), bank(("L1", "T1", "2026-10-15", "10.00")))
    assert cats(r) == ["DUPLICATE_PLATFORM_TXN"] and r.bridge.platform_total == 10 and r.bridge.unexplained == 0


# ------------------------------------------------------------------ refunds
def _refund_case(orig_row, refund_amt="25.000", refund_day="2026-10-15", orig_id="P1"):
    p = platform(orig_row, ("R1", ts(refund_day), REF, OK, refund_amt, orig_id))
    return run(p, bank(("L2", "R1", "2026-10-16", f"-{D(refund_amt):.2f}")))


def test_orphan_refund_is_flagged_with_its_exposure_and_does_not_move_the_bridge():
    r = run(platform(("R1", ts("2026-10-15"), REF, OK, "25.000", "TXN-GHOST")), bank(("L1", "R1", "2026-10-16", "-25.00")))
    assert cats(r) == ["ORPHAN_REFUND"] and r.exceptions.amount.tolist() == [Decimal("-25.000")]
    assert r.bridge.unexplained == 0                                    # cash agrees; it is a control exception


def test_refund_of_a_september_sale_is_not_an_orphan():
    r = _refund_case(("P1", ts("2026-09-28"), PAY, OK, "100.000"))
    assert r.exceptions.empty


def test_refund_of_a_sale_still_in_transit_is_not_an_orphan():
    p = platform(("P1", ts("2026-10-29"), PAY, OK, "100.000"), ("R1", ts("2026-10-31"), REF, OK, "40.000", "P1"))
    r = run(p, bank(("L1", "P1", "2026-11-02", "100.00"), ("L2", "R1", "2026-11-02", "-40.00")))
    assert r.exceptions.empty


def test_refund_with_one_character_corrupted_original_is_an_orphan():
    p = platform(("TXN-AAAA1111", ts("2026-10-10"), PAY, OK, "100.000"),
                 ("R1", ts("2026-10-15"), REF, OK, "25.000", "TXN-AAAA1112"))
    assert "ORPHAN_REFUND" in cats(run(p, bank()))


def test_refund_without_original_id():
    assert "REFUND_MISSING_ORIGINAL_ID" in cats(_refund_case(("P1", ts("2026-10-10"), PAY, OK, "100.000"), orig_id=""))


def test_refund_of_a_failed_payment_is_invalid():
    assert "REFUND_INVALID_TARGET" in cats(_refund_case(("P1", ts("2026-10-10"), PAY, BAD, "100.000")))


def test_refund_exceeding_original_is_flagged():
    assert "REFUND_EXCEEDS_ORIGINAL" in cats(_refund_case(("P1", ts("2026-10-10"), PAY, OK, "100.000"), refund_amt="150.000"))


def test_two_partial_refunds_exceeding_the_original_are_flagged():
    p = platform(("P1", ts("2026-10-10"), PAY, OK, "100.000"),
                 ("R1", ts("2026-10-14"), REF, OK, "60.000", "P1"), ("R2", ts("2026-10-15"), REF, OK, "60.000", "P1"))
    assert cats(run(p, bank())).count("REFUND_EXCEEDS_ORIGINAL") == 2


def test_refund_dated_before_its_original():
    assert "REFUND_BEFORE_ORIGINAL" in cats(_refund_case(("P1", ts("2026-10-12"), PAY, OK, "100.000"), refund_day="2026-10-10"))


# ------------------------------------------------------------------ one-sided records
def test_bank_line_with_no_platform_transaction():
    r = run(platform(), bank(("L1", "GHOST", "2026-10-15", "75.00")))
    assert cats(r) == ["BANK_ONLY"] and r.bridge.unexplained == 75 and r.bridge.unclassified == 0


def test_successful_october_payment_missing_from_bank():
    r = run(platform(("T1", ts("2026-10-14"), PAY, OK, "60.000")), bank())
    assert cats(r) == ["PLATFORM_ONLY"] and r.bridge.unexplained == -60


def test_failed_payment_absent_from_bank_is_fine_but_settled_failed_payment_is_not():
    p = platform(("T1", ts("2026-10-14"), PAY, BAD, "60.000"))
    assert run(p, bank()).exceptions.empty
    r = run(p, bank(("L1", "T1", "2026-10-15", "60.00")))
    assert cats(r) == ["SETTLED_NOT_SUCCESS"] and r.bridge.unexplained == 60


def test_bank_line_before_the_file_window_is_excluded():
    r = run(platform(("T1", ts("2026-10-01"), PAY, OK, "60.000")), bank(("L1", "T1", "2026-09-29", "60.00")))
    assert cats(r) == ["OUT_OF_RANGE_BANK_LINE", "PLATFORM_ONLY"]


def test_batch_control_total_mismatch():
    p = platform(("T1", ts("2026-10-14"), PAY, OK, "60.000"))
    r = run(p, bank(("L1", "T1", "2026-10-15", "60.00")), batches(("BATCH-20261015", "2026-10-15", 1, "99.00")))
    assert cats(r) == ["BATCH_CONTROL_MISMATCH"]
    assert run(p, bank(("L1", "T1", "2026-10-15", "60.00")), batches(("BATCH-20261015", "2026-10-15", 1, "60.00"))).exceptions.empty


# ------------------------------------------------------------------ property: the bridge always accounts for every unit
@pytest.mark.parametrize("seed", range(30))
def test_bridge_is_always_fully_classified(seed):
    rng = random.Random(seed)
    p_rows, b_rows = [], []
    for i in range(60):
        d = date(2026, 9, 25) + timedelta(days=rng.randrange(37))
        typ = REF if rng.random() < 0.1 else PAY
        st = BAD if rng.random() < 0.08 else OK
        amt = str(Decimal(rng.randint(1000, 500000)) / 1000)
        orig = f"T{rng.randrange(60)}" if typ == REF else ""
        if rng.random() < 0.05:
            orig = "GHOST" if typ == REF else ""
        row = (f"T{i}", ts(str(d)), typ, st, amt, orig)
        p_rows.append(row)
        if rng.random() < 0.03:
            p_rows.append(row)                                           # platform duplicate
        if (st == OK or rng.random() < 0.1) and rng.random() < 0.9:
            base = D(amt) if typ == PAY else -D(amt)
            val = round_half_up(base) + rng.choice([0, 0, 0, Decimal("1.00")])
            sd = add_business_days(d, rng.choice([0, 1, 1, 2, 3, 5]))
            b_rows.append((f"L{i}", f"T{i}", str(sd), f"{val:.2f}"))
            if rng.random() < 0.08:
                b_rows.append((f"L{i}x", f"T{i}", str(add_business_days(sd, 1)), f"{val:.2f}"))
    for k in range(3):
        b_rows.append((f"LX{k}", f"UNKNOWN{k}", str(date(2026, 10, 1) + timedelta(days=rng.randrange(33))), "12.34"))
    r = run(platform(*p_rows), bank(*b_rows))
    assert r.bridge.unclassified == 0
