from datetime import date
from decimal import Decimal

import pytest

from src.calendar_utils import add_business_days, business_days_between, period_of, to_ist_date
from src.money import D, dsum, round_half_up


def test_half_up_rounding():
    assert round_half_up("1234.505") == Decimal("1234.51")      # exact half rounds up
    assert round_half_up("0.005") == Decimal("0.01")
    assert round_half_up("0.004") == Decimal("0.00")


def test_floats_are_rejected():
    with pytest.raises(TypeError):
        D(0.1)


def test_float_drift_is_what_we_avoid():
    acc = 0.0
    for _ in range(10):
        acc += 0.1                                               # naive float accumulation drifts (Goldberg 1991)
    assert acc != 1.0
    assert dsum([D("0.1")] * 10) == D("1.0")


def test_rounding_is_invisible_per_line_but_visible_in_the_sum():
    platform = [D("10.005")] * 200
    bank = [round_half_up(x) for x in platform]
    assert all(abs(b - p) <= D("0.005") for b, p in zip(bank, platform))   # every line within tolerance
    assert dsum(bank) - dsum(platform) == D("1.000")                        # ...but the total is off by 1.00


def test_ist_cutoff_differs_from_utc():
    assert to_ist_date("2026-10-31T18:29:59Z") == date(2026, 10, 31)       # 23:59:59 IST
    assert to_ist_date("2026-10-31T18:30:00Z") == date(2026, 11, 1)        # 00:00:00 IST
    assert to_ist_date("2026-09-30T18:30:00Z") == date(2026, 10, 1)


def test_business_day_arithmetic_skips_weekends():
    assert add_business_days(date(2026, 10, 30), 1) == date(2026, 11, 2)   # Fri -> Mon
    assert add_business_days(date(2026, 10, 31), 1) == date(2026, 11, 2)   # Sat -> Mon
    assert add_business_days(date(2026, 10, 31), 2) == date(2026, 11, 3)
    assert add_business_days(date(2026, 11, 1), 1) == date(2026, 11, 2)    # Sun -> Mon


def test_business_days_between():
    assert business_days_between(date(2026, 10, 31), date(2026, 11, 2)) == 1
    assert business_days_between(date(2026, 10, 14), date(2026, 10, 19)) == 3
    assert business_days_between(date(2026, 10, 15), date(2026, 10, 15)) == 0
    assert business_days_between(date(2026, 10, 15), date(2026, 10, 14)) == -1


def test_period_of():
    assert [period_of(date(2026, 9, 30)), period_of(date(2026, 10, 1)),
            period_of(date(2026, 10, 31)), period_of(date(2026, 11, 1))] == ["PRE", "IN", "IN", "POST"]
