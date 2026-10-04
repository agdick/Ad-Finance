"""Money helpers. All amounts are stored as integer cents (CAD). Never use floats."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


class MoneyError(ValueError):
    pass


_STRIP = re.compile(r"[\s$,]|CAD|C\$", re.IGNORECASE)


def parse_cents(text: str | int | Decimal | None) -> int:
    """Parse a user- or file-supplied amount into integer cents.

    Accepts "1,234.56", "$12", "-12.30", "(12.30)" (negative), "12.30-", "12.30 CR" (positive)
    and "12.30 DR" (negative).
    """
    if text is None:
        raise MoneyError("empty amount")
    if isinstance(text, int):
        return text * 100
    if isinstance(text, Decimal):
        return decimal_to_cents(text)
    s = str(text).strip()
    if not s:
        raise MoneyError("empty amount")
    negative = False
    upper = s.upper()
    if upper.endswith("CR"):
        s = s[:-2]
    elif upper.endswith("DR"):
        s, negative = s[:-2], True
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        s, negative = s[1:-1], not negative
    if s.endswith("-"):
        s, negative = s[:-1], not negative
    s = _STRIP.sub("", s).replace("−", "-")
    if s.startswith("-"):
        s, negative = s[1:], not negative
    elif s.startswith("+"):
        s = s[1:]
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise MoneyError(f"not a number: {text!r}") from exc
    cents = decimal_to_cents(value)
    return -cents if negative else cents


def decimal_to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_cents(cents: int | None, *, signed: bool = False, blank: str = "") -> str:
    """Format cents as CAD, e.g. 123456 -> "$1,234.56", -500 -> "−$5.00"."""
    if cents is None:
        return blank
    sign = ""
    if cents < 0:
        sign = "−"
    elif signed and cents > 0:
        sign = "+"
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{frac:02d}"


def cents_to_input(cents: int | None) -> str:
    """Format cents for an <input> value: plain decimal, no symbols."""
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def percent(part: int, whole: int) -> int | None:
    """Integer percentage, rounded down. None when whole is zero/unset."""
    if not whole:
        return None
    return (part * 100) // whole
