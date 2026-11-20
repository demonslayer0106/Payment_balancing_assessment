"""Single source of truth for period, time zone and tolerances."""
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

SEED = 20261031
IST = ZoneInfo("Asia/Kolkata")          # UTC+5:30, no DST
UTC = timezone.utc

PERIOD_START = date(2026, 10, 1)        # reporting period = October 2026 (IST dates)
PERIOD_END = date(2026, 10, 31)
LOOKBACK_START = date(2026, 9, 25)      # platform extract starts here
BANK_START = date(2026, 10, 1)          # bank file coverage
BANK_END = date(2026, 11, 3)            # long enough to see Oct 31 sales settle (2 business days)
EXTRACT_END_UTC = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)

ROUNDING_TOLERANCE = Decimal("0.005")   # |bank - platform| <= half a paisa is "rounding"
MIN_LAG_BD = 1                          # settlement lag, in business days after the IST txn date
MAX_LAG_BD = 2
CURRENCY = "INR"
