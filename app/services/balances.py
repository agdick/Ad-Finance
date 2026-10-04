from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, BalanceSnapshot, utcnow


def record_balance(db: Session, account: Account, balance: int | None, on: dt.date) -> None:
    """Set an account's current balance and keep one history snapshot per day."""
    if balance is None:
        return
    account.current_balance = balance
    account.balance_updated_at = utcnow()
    snap = db.scalar(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == on))
    if snap:
        snap.balance = balance
    else:
        db.add(BalanceSnapshot(account_id=account.id, date=on, balance=balance))


def balance_on_or_after(db: Session, account_id: int, since: dt.date) -> BalanceSnapshot | None:
    return db.scalar(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.date >= since)
        .order_by(BalanceSnapshot.date)
        .limit(1)
    )
