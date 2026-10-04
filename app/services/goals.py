"""Savings and debt-payoff goals: progress and a simple projection at the current pace."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Goal
from ..money import percent
from .balances import balance_on_or_after

PACE_WINDOW_DAYS = 90


@dataclass
class GoalProgress:
    progress: int  # saved so far / paid down so far (cents)
    target: int
    remaining: int
    pct: int
    owed: int | None  # debt goals: current balance owed
    pace_per_month: int | None  # cents per ~30 days at the recent pace; None = not enough history
    projected_date: dt.date | None
    on_track: bool | None  # vs. target_date; None when unknown


def _pace_from_snapshots(db: Session, goal: Goal, today: dt.date) -> tuple[int, int] | None:
    """Sum of balance change over the window across linked accounts -> (delta cents, days)."""
    since = today - dt.timedelta(days=PACE_WINDOW_DAYS)
    delta, span = 0, 0
    for acct in goal.accounts:
        first = balance_on_or_after(db, acct.id, since)
        if first is None or acct.current_balance is None:
            continue
        days = (today - first.date).days
        if days <= 0:
            continue
        delta += acct.current_balance - first.balance
        span = max(span, days)
    return (delta, span) if span >= 7 else None


def goal_progress(db: Session, goal: Goal, today: dt.date) -> GoalProgress:
    owed = None
    pace_per_day: float | None = None

    if goal.type == "debt":
        owed = sum(max(a.current_balance or 0, 0) for a in goal.accounts)
        progress = max(goal.target_amount - owed, 0)
        remaining = owed
        snap = _pace_from_snapshots(db, goal, today)
        if snap:
            delta, days = snap
            pace_per_day = -delta / days  # balance going down = paying off
    else:
        if goal.progress_source == "contributions":
            progress = sum(c.amount for c in goal.contributions)
            recent = [c for c in goal.contributions if c.date >= today - dt.timedelta(days=PACE_WINDOW_DAYS)]
            if recent:
                first = min(c.date for c in goal.contributions)
                days = max((today - max(first, today - dt.timedelta(days=PACE_WINDOW_DAYS))).days, 30)
                pace_per_day = sum(c.amount for c in recent) / days
        else:
            progress = sum(a.current_balance or 0 for a in goal.accounts)
            snap = _pace_from_snapshots(db, goal, today)
            if snap:
                delta, days = snap
                pace_per_day = delta / days
        remaining = max(goal.target_amount - progress, 0)

    projected = None
    if remaining == 0:
        projected = today
    elif pace_per_day and pace_per_day > 0:
        days_left = remaining / pace_per_day
        if days_left < 365 * 100:
            projected = today + dt.timedelta(days=int(days_left) + 1)

    on_track = None
    if goal.target_date:
        on_track = projected is not None and projected <= goal.target_date

    return GoalProgress(
        progress=progress,
        target=goal.target_amount,
        remaining=remaining,
        pct=min(percent(progress, goal.target_amount) or 0, 100),
        owed=owed,
        pace_per_month=int(pace_per_day * 30) if pace_per_day is not None else None,
        projected_date=projected,
        on_track=on_track,
    )
