from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Account, ProviderConnection, Transaction
from ..security import verify_csrf
from ..services import alerts, pipeline
from ..services.export import full_json, transactions_csv
from ..services.settings_store import get_setting, parse_thresholds, set_setting
from .common import flash, redirect, render, safe_next

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    connections = db.scalars(select(ProviderConnection).order_by(ProviderConnection.created_at)).all()
    counts = {
        c.id: db.scalar(select(func.count()).select_from(Account).where(Account.connection_id == c.id)) for c in connections
    }
    return render(
        request,
        db,
        "settings.html",
        {
            "plaid_configured": s.plaid_configured,
            "plaid_env": s.plaid_env,
            "plaid_products": ["transactions", *s.plaid_optional_products],
            "connections": connections,
            "account_counts": counts,
            "active_items": sum(1 for c in connections if c.status != "disconnected"),
            "sync_interval": s.sync_interval_hours,
            "scheduler_enabled": s.scheduler_enabled,
            "thresholds": get_setting(db, "alert_thresholds"),
            "txn_count": db.scalar(select(func.count()).select_from(Transaction)),
            "timezone": s.timezone,
        },
    )


@router.post("/settings/alerts", dependencies=[Depends(verify_csrf)])
def save_alert_settings(request: Request, thresholds: str = Form(""), db: Session = Depends(get_db)):
    values = parse_thresholds(thresholds)
    if not values:
        flash(request, "Enter one or more percentages, e.g. 80, 100", "error")
        return redirect("/settings")
    set_setting(db, "alert_thresholds", ",".join(map(str, values)))
    pipeline.after_edit(db)
    db.commit()
    flash(request, "Alert thresholds saved.")
    return redirect("/settings")


@router.post("/alerts/{alert_id}/dismiss", dependencies=[Depends(verify_csrf)])
def dismiss_alert(alert_id: int, next: str = Form("/"), db: Session = Depends(get_db)):
    alerts.dismiss(db, alert_id)
    db.commit()
    return redirect(safe_next(next))


def _stamp() -> str:
    return dt.date.today().isoformat()


@router.get("/export/transactions.csv")
def export_csv(db: Session = Depends(get_db)):
    return Response(
        transactions_csv(db),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="transactions-{_stamp()}.csv"'},
    )


@router.get("/export/data.json")
def export_json(db: Session = Depends(get_db)):
    return Response(
        full_json(db),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="finance-export-{_stamp()}.json"'},
    )
