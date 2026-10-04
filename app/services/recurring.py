"""Recurring bills/subscriptions: detection, tracking, and flags (missed / changed / new)."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RecurringItem, Transaction
from .payees import payee_key

FREQUENCIES = {
    "weekly": "Weekly",
    "biweekly": "Every 2 weeks",
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "yearly": "Yearly",
}
# (min days, max days, nominal days) for classifying observed intervals.
_INTERVALS = {
    "weekly": (6, 8, 7),
    "biweekly": (13, 16, 14),
    "monthly": (27, 33, 30),
    "quarterly": (85, 97, 91),
    "yearly": (355, 375, 365),
}
LOOKBACK_DAYS = 400
AMOUNT_TOLERANCE = 0.20  # detection: amounts within 20% of the median count as "similar"


def advance(d: dt.date, frequency: str, n: int = 1) -> dt.date:
    if frequency == "weekly":
        return d + dt.timedelta(weeks=n)
    if frequency == "biweekly":
        return d + dt.timedelta(weeks=2 * n)
    if frequency == "monthly":
        return d + relativedelta(months=n)
    if frequency == "quarterly":
        return d + relativedelta(months=3 * n)
    if frequency == "yearly":
        return d + relativedelta(years=n)
    raise ValueError(f"unknown frequency {frequency!r}")


def classify(intervals: list[int]) -> str | None:
    if not intervals:
        return None
    median = statistics.median(intervals)
    for freq, (lo, hi, _nominal) in _INTERVALS.items():
        if lo <= median <= hi:
            within = sum(1 for i in intervals if lo <= i <= hi)
            return freq if within / len(intervals) >= 0.75 else None
    return None


def grace_days(frequency: str) -> int:
    return 2 if frequency == "weekly" else 4


def detect_recurring(db: Session, today: dt.date) -> list[RecurringItem]:
    """Find payees that repeat at a regular interval with a similar amount and create
    'detected' items for any that aren't already tracked (or dismissed)."""
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    txns = db.scalars(
        select(Transaction)
        .where(Transaction.date >= since, Transaction.is_transfer.is_(False), Transaction.is_pending.is_(False))
        .order_by(Transaction.date)
    ).all()
    known = {payee_key(i.payee_match) for i in db.scalars(select(RecurringItem)) if i.payee_match}

    groups: dict[tuple[str, bool], list[Transaction]] = defaultdict(list)
    for t in txns:
        key = payee_key(t.payee_clean)
        if key and t.amount != 0:
            groups[(key, t.amount < 0)].append(t)

    created = []
    for (key, _outflow), group in groups.items():
        if key in known:
            continue
        # One occurrence per date (a payee charged twice on one day is not two periods).
        by_date: dict[dt.date, Transaction] = {}
        for t in group:
            by_date.setdefault(t.date, t)
        dates = sorted(by_date)
        if len(dates) < 3 and not (len(dates) == 2 and (dates[1] - dates[0]).days >= 355):
            continue
        freq = classify([(b - a).days for a, b in zip(dates, dates[1:])])
        if freq is None:
            continue
        amounts = [abs(by_date[d].amount) for d in dates]
        median_amt = statistics.median(amounts)
        similar = sum(1 for a in amounts if abs(a - median_amt) <= AMOUNT_TOLERANCE * median_amt)
        if similar / len(amounts) < 0.75:
            continue
        last = by_date[dates[-1]]
        if advance(last.date, freq, 2) < today:  # stopped long ago; not current
            continue
        account_id = Counter(by_date[d].account_id for d in dates).most_common(1)[0][0]
        item = RecurringItem(
            name=last.payee_clean,
            payee_match=last.payee_clean,
            amount=last.amount,
            frequency=freq,
            next_due=advance(last.date, freq),
            category_id=last.category_id,
            account_id=account_id,
            status="detected",
            origin="detected",
            last_seen=last.date,
            last_amount=last.amount,
        )
        db.add(item)
        created.append(item)
        known.add(key)
    db.flush()
    return created


def latest_match(db: Session, item: RecurringItem, today: dt.date) -> Transaction | None:
    if not item.payee_match:
        return None
    key = payee_key(item.payee_match)
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    q = select(Transaction).where(Transaction.is_transfer.is_(False), Transaction.date >= since)
    if item.account_id:
        q = q.where(Transaction.account_id == item.account_id)
    q = q.where(Transaction.amount < 0) if item.amount < 0 else q.where(Transaction.amount > 0)
    for t in db.scalars(q.order_by(Transaction.date.desc(), Transaction.id.desc())):
        if payee_key(t.payee_clean) == key or key in payee_key(t.payee_raw):
            return t
    return None


def refresh_item(db: Session, item: RecurringItem, today: dt.date) -> None:
    """Move last_seen / next_due forward when a new matching transaction has arrived."""
    t = latest_match(db, item, today)
    if t is None or (item.last_seen and t.date <= item.last_seen):
        return
    item.last_seen, item.last_amount = t.date, t.amount
    nxt = item.next_due or t.date
    while nxt <= t.date + dt.timedelta(days=grace_days(item.frequency)):
        nxt = advance(nxt, item.frequency)
    item.next_due = nxt


def refresh_all(db: Session, today: dt.date) -> None:
    for item in db.scalars(select(RecurringItem).where(RecurringItem.status != "dismissed")):
        refresh_item(db, item, today)
    detect_recurring(db, today)


@dataclass
class Flags:
    missed: bool
    changed: bool
    new: bool


def flags(item: RecurringItem, today: dt.date) -> Flags:
    missed = bool(item.next_due and item.next_due + dt.timedelta(days=grace_days(item.frequency)) < today)
    changed = False
    if item.last_amount is not None:
        diff = abs(item.last_amount - item.amount)
        changed = diff > max(100, abs(item.amount) * 5 // 100)
    return Flags(missed=missed, changed=changed, new=item.status == "detected")


@dataclass
class Occurrence:
    item: RecurringItem
    date: dt.date
    amount: int


def upcoming(db: Session, today: dt.date, days: int = 30, bills_only: bool = True) -> list[Occurrence]:
    end = today + dt.timedelta(days=days)
    out = []
    for item in db.scalars(select(RecurringItem).where(RecurringItem.status == "confirmed")):
        if bills_only and item.amount >= 0:
            continue
        d = item.next_due
        if d is None:
            continue
        while d < today:  # missed occurrences are flagged separately
            d = advance(d, item.frequency)
        while d <= end:
            out.append(Occurrence(item, d, item.amount))
            d = advance(d, item.frequency)
    return sorted(out, key=lambda o: (o.date, o.item.name.lower()))
