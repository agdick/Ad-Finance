from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import alerts
from ..services.budget import month_summary
from ..services.dates import current_month, month_bounds, parse_month, today
from .common import render, review_count

router = APIRouter()


@router.get("/")
def home(request: Request, month: str | None = None, db: Session = Depends(get_db)):
    month = parse_month(month)
    summary = month_summary(db, month)
    start, end = month_bounds(month)
    is_current = month == current_month()
    days_left = (end - today()).days + 1 if is_current else None
    return render(
        request,
        db,
        "home.html",
        {
            "month": month,
            "summary": summary,
            "is_current": is_current,
            "days_left": days_left,
            "alerts": alerts.active_alerts(db) if is_current else [],
            "review_count": review_count(db),
            "month_start": start,
            "month_end": end,
        },
    )
