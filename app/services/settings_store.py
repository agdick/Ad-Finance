"""User-editable app settings stored in the database (as opposed to env configuration)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AppSetting

DEFAULTS = {
    "alert_thresholds": "80,100",
}


def get_setting(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else DEFAULTS.get(key, "")


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def parse_thresholds(text: str | None) -> list[int]:
    """"80, 100" -> [80, 100]. Ignores junk; values must be 1..500."""
    out = set()
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip().rstrip("%")
        if part.isdigit() and 0 < int(part) <= 500:
            out.add(int(part))
    return sorted(out)
