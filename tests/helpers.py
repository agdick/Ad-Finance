from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Account, Category, Transaction
from app.services.payees import clean_payee


def account(db, name="Chequing", type_="chequing", source="manual", **kw) -> Account:
    a = Account(name=name, type=type_, source=source, **kw)
    db.add(a)
    db.flush()
    return a


def cat(db, name) -> Category:
    return db.scalar(select(Category).where(Category.name == name))


def txn(db, acct, date, amount, payee="Store", category=None, confirmed=True, source="manual", **kw) -> Transaction:
    t = Transaction(
        account_id=acct.id,
        date=date if isinstance(date, dt.date) else dt.date.fromisoformat(date),
        amount=amount,
        payee_raw=payee,
        payee_clean=clean_payee(payee),
        category_id=category.id if category else None,
        category_status="confirmed" if confirmed else "suggested",
        source=source,
        **kw,
    )
    db.add(t)
    db.flush()
    return t


def count(db) -> int:
    return len(db.scalars(select(Transaction)).all())
