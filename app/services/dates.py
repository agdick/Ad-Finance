from __future__ import annotations

import calendar
import datetime as dt
import re

from ..config import get_settings


def today() -> dt.date:
    return dt.datetime.now(get_settings().tz).date()


def month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def current_month() -> str:
    return month_key(today())


_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def parse_month(value: str | None) -> str:
    """Validate a YYYY-MM string, falling back to the current month."""
    if value:
        m = _MONTH.match(value)
        if m and 1 <= int(m.group(2)) <= 12:
            return value
    return current_month()


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    year, mon = int(month[:4]), int(month[5:7])
    return dt.date(year, mon, 1), dt.date(year, mon, calendar.monthrange(year, mon)[1])


def shift_month(month: str, delta: int) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    idx = year * 12 + (mon - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_label(month: str) -> str:
    first, _ = month_bounds(month)
    return first.strftime("%B %Y")
