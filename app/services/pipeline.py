"""Work that runs after any new data arrives (sync, CSV import, manual entry)."""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from ..models import Transaction
from . import alerts, categorize, recurring, transfers
from .dates import today


def after_ingest(db: Session, new_txns: Sequence[Transaction] = ()) -> None:
    categorize.suggest_many(db, [t for t in new_txns if t.category_status != "confirmed"])
    db.flush()
    since = min((t.date for t in new_txns), default=None)
    transfers.detect_transfers(db, since=since)
    recurring.refresh_all(db, today())
    alerts.evaluate_alerts(db)


def after_edit(db: Session) -> None:
    """Category/transfer edits can move spending across a threshold."""
    db.flush()
    alerts.evaluate_alerts(db)
