"""Starter categories, created once on first start. Rename/archive/add freely in the UI;
limits start empty so nothing is assumed about the budget."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Category

STARTER_EXPENSE = [
    "Housing",
    "Utilities",
    "Phone & Internet",
    "Groceries",
    "Dining Out",
    "Transportation",
    "Insurance",
    "Health",
    "Personal Care",
    "Shopping",
    "Entertainment",
    "Subscriptions",
    "Travel",
    "Gifts & Donations",
    "Fees & Interest",
    "Miscellaneous",
]
STARTER_INCOME = ["Paycheque", "Other Income"]


def seed_categories(db: Session) -> bool:
    if db.scalar(select(func.count()).select_from(Category)):
        return False
    for i, name in enumerate(STARTER_EXPENSE):
        db.add(Category(name=name, kind="expense", sort_order=i))
    for i, name in enumerate(STARTER_INCOME):
        db.add(Category(name=name, kind="income", sort_order=100 + i))
    db.commit()
    return True
