"""Exact money arithmetic. Floats are rejected on purpose: they would *create* rounding drift."""
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0")


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        raise TypeError("floats are not allowed for money; pass a str, int or Decimal")
    return Decimal(str(x).strip())


def round_half_up(x, places: int = 2) -> Decimal:
    return D(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def dsum(values) -> Decimal:
    total = ZERO
    for v in values:
        total += v
    return total


def fmt(x, places: int = 2) -> str:
    return f"{D(x):.{places}f}"
