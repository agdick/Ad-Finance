"""Category-limit alerts. Each (category, month, threshold) fires at most once; the unique
constraint on the alerts table enforces it. A new month starts with a clean slate.

Delivery is pluggable: the in-app banner reads the alerts table directly; additional
channels (email, push) implement `Notifier` and are added with `register_notifier`.
"""
from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Alert, Category, utcnow
from .budget import month_summary
from .dates import current_month
from .settings_store import get_setting, parse_thresholds

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, alert: Alert, category: Category) -> None: ...


_NOTIFIERS: list[Notifier] = []


def register_notifier(notifier: Notifier) -> None:
    _NOTIFIERS.append(notifier)


def clear_notifiers() -> None:
    _NOTIFIERS.clear()


def thresholds_for(db: Session, category: Category) -> list[int]:
    own = parse_thresholds(category.alert_thresholds)
    return own or parse_thresholds(get_setting(db, "alert_thresholds"))


def evaluate_alerts(db: Session, month: str | None = None) -> list[Alert]:
    """Fire any newly crossed thresholds for the month (default: current month)."""
    month = month or current_month()
    summary = month_summary(db, month)
    existing = {(a.category_id, a.threshold) for a in db.scalars(select(Alert).where(Alert.month == month))}
    fired = []
    for row in summary.rows:
        if not row.limit or row.limit <= 0 or row.category_id is None:
            continue
        category = db.get(Category, row.category_id)
        for threshold in thresholds_for(db, category):
            if (row.category_id, threshold) in existing:
                continue
            if row.spent * 100 >= threshold * row.limit:
                alert = Alert(category_id=row.category_id, month=month, threshold=threshold, spent=row.spent, limit=row.limit)
                db.add(alert)
                fired.append(alert)
                existing.add((row.category_id, threshold))
    if fired:
        db.flush()
        for alert in fired:
            for notifier in _NOTIFIERS:
                try:
                    notifier.send(alert, alert.category or db.get(Category, alert.category_id))
                except Exception:  # a broken channel must never break budgeting
                    log.exception("Alert notifier %s failed", type(notifier).__name__)
    return fired


def active_alerts(db: Session, month: str | None = None) -> list[Alert]:
    month = month or current_month()
    return list(
        db.scalars(
            select(Alert)
            .where(Alert.month == month, Alert.dismissed_at.is_(None))
            .order_by(Alert.threshold.desc(), Alert.fired_at.desc())
        )
    )


def dismiss(db: Session, alert_id: int) -> None:
    alert = db.get(Alert, alert_id)
    if alert and alert.dismissed_at is None:
        alert.dismissed_at = utcnow()
