"""IST conversion and business-day arithmetic. Every cutoff in the system goes through here."""
from datetime import date, datetime, timedelta

from . import config as C


def parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=C.UTC)


def to_ist_date(ts) -> date:
    dt = parse_utc(ts) if isinstance(ts, str) else ts
    return dt.astimezone(C.IST).date()


def is_business_day(d: date) -> bool:
    return d.weekday() < 5              # Mon-Fri; no holiday calendar (stated assumption)


def add_business_days(d: date, n: int) -> date:
    """n-th business day strictly after d (n >= 0). A weekend start rolls to Monday."""
    cur, remaining = d, n
    while remaining > 0:
        cur += timedelta(days=1)
        if is_business_day(cur):
            remaining -= 1
    return cur


def business_days_between(start: date, end: date) -> int:
    """Number of weekdays in (start, end]. Negative if end is before start."""
    if end == start:
        return 0
    sign, a, b = (1, start, end) if end > start else (-1, end, start)
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if is_business_day(cur):
            n += 1
    return sign * n


def period_of(d: date) -> str:
    """PRE (before October), IN (October) or POST (after October)."""
    if d < C.PERIOD_START:
        return "PRE"
    return "IN" if d <= C.PERIOD_END else "POST"
