"""Duplicate detection within and across data sources.

* CSV re-imports: every CSV row gets a fingerprint (date|amount|normalized description).
  Importing a file whose fingerprints already exist N times in that account inserts only
  the surplus, so re-importing is a no-op while genuine same-day repeats are kept.
* Cross-source: a bank-synced transaction and a CSV/manual one for the same account with
  the same amount within a few days are the same transaction. Whichever arrives second is
  merged into the first (it gains the other's external_id / fingerprint) instead of inserted.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Transaction

CROSS_SOURCE_WINDOW_DAYS = 3
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def fingerprint(date: dt.date, amount: int, description: str) -> str:
    desc = _NON_ALNUM.sub(" ", (description or "").lower()).strip()
    return f"{date.isoformat()}|{amount}|{desc}"[:512]


def _closest(candidates: list[Transaction], date: dt.date) -> Transaction | None:
    best = None
    for t in candidates:
        if abs((t.date - date).days) > CROSS_SOURCE_WINDOW_DAYS:
            continue
        if best is None or abs((t.date - date).days) < abs((best.date - date).days):
            best = t
    return best


def match_for_csv_row(db: Session, account_id: int, date: dt.date, amount: int, exclude: set[int]) -> Transaction | None:
    """An existing synced/manual transaction that this CSV row duplicates."""
    window = dt.timedelta(days=CROSS_SOURCE_WINDOW_DAYS)
    candidates = db.scalars(
        select(Transaction)
        .where(
            Transaction.account_id == account_id,
            Transaction.amount == amount,
            Transaction.source != "csv",
            Transaction.import_fingerprint.is_(None),
            Transaction.date.between(date - window, date + window),
        )
        .order_by(Transaction.id)
    ).all()
    return _closest([c for c in candidates if c.id not in exclude], date)


def match_for_synced(db: Session, account_id: int, date: dt.date, amount: int) -> Transaction | None:
    """An existing CSV/manual transaction that a newly synced transaction duplicates."""
    window = dt.timedelta(days=CROSS_SOURCE_WINDOW_DAYS)
    candidates = db.scalars(
        select(Transaction)
        .where(
            Transaction.account_id == account_id,
            Transaction.amount == amount,
            Transaction.external_id.is_(None),
            Transaction.source.in_(("csv", "manual")),
            Transaction.date.between(date - window, date + window),
        )
        .order_by(Transaction.id)
    ).all()
    return _closest(candidates, date)
