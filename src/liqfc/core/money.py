"""Fixed-precision money arithmetic.

Amounts are signed Python/NumPy integers in currency minor units (USD cents, JPY yen).
FX rates are integers scaled by 10^8 (``rate_e8``). All rounding is ROUND_HALF_EVEN and is
performed with exact integer arithmetic -- binary floating point is never used for money.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

RATE_DECIMALS = 8
RATE_SCALE = 10**RATE_DECIMALS
BP_SCALE = 10_000


def div_half_even(n: int, d: int) -> int:
    """Exact integer division rounded half-to-even (d > 0)."""
    if d <= 0:
        raise ValueError("divisor must be positive")
    q, r = divmod(int(n), int(d))
    twice = 2 * r
    if twice > d or (twice == d and q % 2 == 1):
        q += 1
    return q


def parse_rate(text: str) -> int:
    value = Decimal(str(text)).scaleb(RATE_DECIMALS)
    if value != value.to_integral_value():
        raise ValueError(f"rate {text} has more than {RATE_DECIMALS} decimals")
    if value <= 0:
        raise ValueError(f"rate {text} must be positive")
    return int(value)


def format_rate(rate_e8: int) -> str:
    return f"{rate_e8 // RATE_SCALE}.{rate_e8 % RATE_SCALE:0{RATE_DECIMALS}d}"


def to_minor(amount, minor_units: int) -> int:
    """Major-unit Decimal/str/int -> integer minor units (half-even)."""
    return int((Decimal(str(amount)).scaleb(minor_units)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def format_minor(minor: int, minor_units: int) -> str:
    return str(Decimal(int(minor)).scaleb(-minor_units).quantize(Decimal(1).scaleb(-minor_units)))


def convert(amount_minor: int, src_units: int, rate_e8: int, dst_units: int) -> int:
    """amount(src) * rate -> dst minor units. rate = dst per 1 src."""
    return div_half_even(int(amount_minor) * int(rate_e8) * 10**dst_units, RATE_SCALE * 10**src_units)


def convert_inverse(amount_minor: int, src_units: int, rate_e8: int, dst_units: int) -> int:
    """amount(reporting) / rate -> local minor units. rate = reporting per 1 local."""
    return div_half_even(int(amount_minor) * RATE_SCALE * 10**dst_units, int(rate_e8) * 10**src_units)


def convert_many(amounts, src_units, rates_e8, dst_units: int) -> list[int]:
    """Vector form of :func:`convert`; src_units/rates may be scalars or sequences."""
    n = len(amounts)
    su = src_units if hasattr(src_units, "__len__") else [src_units] * n
    rt = rates_e8 if hasattr(rates_e8, "__len__") else [rates_e8] * n
    return [convert(a, s, r, dst_units) for a, s, r in zip(amounts, su, rt)]


def apply_bp(amount_minor: int, bp: int) -> int:
    """amount * bp / 10000, half-even (bp=9000 keeps 90%)."""
    return div_half_even(int(amount_minor) * int(bp), BP_SCALE)


def shock_rate(rate_e8: int, shock_bp: int) -> int:
    """Apply an FX shock in basis points: -500 => rate * 0.95."""
    return div_half_even(int(rate_e8) * (BP_SCALE + int(shock_bp)), BP_SCALE)
