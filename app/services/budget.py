"""Budget vs. actual for a calendar month.

Spending = outflows (net of refunds) in expense categories, excluding transfers.
Income (paycheques etc.) is reported separately and never affects category math, so a
month with three bi-weekly paycheques budgets exactly like any other month.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Category, Transaction
from ..money import percent
from .dates import month_bounds


@dataclass
class SpendingLine:
    """One budget-relevant slice of a transaction. Splits would yield several per transaction."""

    transaction_id: int
    category_id: int | None
    amount: int
    confirmed: bool


@dataclass
class BudgetRow:
    category_id: int | None
    name: str
    limit: int | None
    spent: int = 0
    has_unconfirmed: bool = False
    count: int = 0

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else self.limit - self.spent

    @property
    def pct(self) -> int | None:
        return percent(self.spent, self.limit) if self.limit else None

    @property
    def level(self) -> str:
        p = self.pct
        if p is None:
            return "none"
        return "over" if p >= 100 else "warn" if p >= 80 else "ok"


@dataclass
class MonthSummary:
    month: str
    rows: list[BudgetRow] = field(default_factory=list)
    uncategorized: BudgetRow | None = None
    income: int = 0
    income_count: int = 0
    income_has_unconfirmed: bool = False

    @property
    def total_limit(self) -> int:
        return sum(r.limit or 0 for r in self.rows)

    @property
    def total_spent(self) -> int:
        return sum(r.spent for r in self.rows) + (self.uncategorized.spent if self.uncategorized else 0)

    @property
    def budgeted_spent(self) -> int:
        """Spending in categories that have a limit (comparable with total_limit)."""
        return sum(r.spent for r in self.rows if r.limit)

    @property
    def total_pct(self) -> int | None:
        return percent(self.budgeted_spent, self.total_limit)

    @property
    def has_unconfirmed(self) -> bool:
        return any(r.has_unconfirmed for r in self.rows) or bool(self.uncategorized and self.uncategorized.has_unconfirmed)

    def row_for(self, category_id: int) -> BudgetRow | None:
        return next((r for r in self.rows if r.category_id == category_id), None)


def spending_lines(db: Session, start: dt.date, end: dt.date) -> list[SpendingLine]:
    txns = db.execute(
        select(Transaction.id, Transaction.category_id, Transaction.amount, Transaction.category_status).where(
            Transaction.date >= start,
            Transaction.date <= end,
            Transaction.is_transfer.is_(False),
        )
    ).all()
    return [SpendingLine(t.id, t.category_id, t.amount, t.category_status == "confirmed") for t in txns]


def month_summary(db: Session, month: str) -> MonthSummary:
    start, end = month_bounds(month)
    categories = {c.id: c for c in db.scalars(select(Category))}
    summary = MonthSummary(month=month)
    by_cat: dict[int, BudgetRow] = {}
    uncategorized = BudgetRow(category_id=None, name="Uncategorized", limit=None)

    for line in spending_lines(db, start, end):
        cat = categories.get(line.category_id) if line.category_id else None
        if cat is not None and cat.kind == "income":
            summary.income += line.amount
            summary.income_count += 1
            summary.income_has_unconfirmed |= not line.confirmed
            continue
        if cat is None:
            if line.amount > 0:  # unknown money in: show as income, never as negative spending
                summary.income += line.amount
                summary.income_count += 1
                summary.income_has_unconfirmed = True
                continue
            row = uncategorized
        else:
            row = by_cat.setdefault(cat.id, BudgetRow(cat.id, cat.name, cat.monthly_limit))
        row.spent -= line.amount
        row.count += 1
        row.has_unconfirmed |= not line.confirmed

    for cat in sorted(categories.values(), key=lambda c: (c.sort_order, c.name.lower())):
        if cat.kind != "expense":
            continue
        row = by_cat.get(cat.id)
        if row is None and (cat.archived or not cat.monthly_limit):
            continue  # nothing to show
        summary.rows.append(row or BudgetRow(cat.id, cat.name, cat.monthly_limit))
    # Over-budget and closest-to-limit first; then unbudgeted spending.
    summary.rows.sort(key=lambda r: (r.limit is None, -(r.pct or 0), -r.spent))
    if uncategorized.count:
        summary.uncategorized = uncategorized
    return summary
