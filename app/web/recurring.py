from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import RecurringItem
from ..money import MoneyError, parse_cents
from ..security import verify_csrf
from ..services import recurring as rec
from ..services.dates import today
from .common import active_accounts, active_categories, flash, redirect, render

router = APIRouter()


@router.get("/recurring")
def recurring_page(request: Request, db: Session = Depends(get_db)):
    now = today()
    items = db.scalars(select(RecurringItem).where(RecurringItem.status != "dismissed")).all()
    rows = [(i, rec.flags(i, now)) for i in items]
    rows.sort(key=lambda r: (r[0].status != "detected", not r[1].missed, r[0].next_due or dt.date.max))
    upcoming = rec.upcoming(db, now, 30)
    dismissed = db.scalars(select(RecurringItem).where(RecurringItem.status == "dismissed").order_by(RecurringItem.name)).all()
    monthly_equiv = sum(_monthly(i) for i in items if i.status == "confirmed" and i.amount < 0)
    return render(
        request,
        db,
        "recurring.html",
        {
            "rows": rows,
            "upcoming": upcoming,
            "upcoming_total": sum(o.amount for o in upcoming),
            "dismissed": dismissed,
            "monthly_equiv": monthly_equiv,
            "frequencies": rec.FREQUENCIES,
            "accounts": active_accounts(db),
            "categories": active_categories(db),
            "item": None,
        },
    )


def _monthly(item: RecurringItem) -> int:
    factor = {"weekly": 52 / 12, "biweekly": 26 / 12, "monthly": 1, "quarterly": 1 / 3, "yearly": 1 / 12}[item.frequency]
    return int(item.amount * factor)


def _apply_form(item: RecurringItem, name, amount, direction, frequency, next_due, category_id, account_id, payee_match):
    if not name.strip() or frequency not in rec.FREQUENCIES:
        raise ValueError("Name and frequency are required.")
    try:
        cents = abs(parse_cents(amount))
    except MoneyError:
        raise ValueError("Enter an amount.") from None
    try:
        due = dt.date.fromisoformat(next_due) if next_due else None
    except ValueError:
        raise ValueError("Next due date is not a valid date.") from None
    item.name = name.strip()[:128]
    item.amount = cents if direction == "in" else -cents
    item.frequency = frequency
    item.next_due = due
    item.category_id = int(category_id) if category_id.isdigit() else None
    item.account_id = int(account_id) if account_id.isdigit() else None
    item.payee_match = (payee_match.strip() or item.name)[:256]


@router.post("/recurring", dependencies=[Depends(verify_csrf)])
def create_item(
    request: Request,
    name: str = Form(""),
    amount: str = Form(""),
    direction: str = Form("out"),
    frequency: str = Form("monthly"),
    next_due: str = Form(""),
    category_id: str = Form(""),
    account_id: str = Form(""),
    payee_match: str = Form(""),
    db: Session = Depends(get_db),
):
    item = RecurringItem(status="confirmed", origin="manual")
    try:
        _apply_form(item, name, amount, direction, frequency, next_due, category_id, account_id, payee_match)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect("/recurring")
    db.add(item)
    db.flush()
    rec.refresh_item(db, item, today())
    db.commit()
    flash(request, f"Added {item.name}.")
    return redirect("/recurring")


@router.get("/recurring/{item_id}/edit")
def edit_item(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = db.get(RecurringItem, item_id)
    if item is None:
        raise HTTPException(404)
    return render(
        request,
        db,
        "recurring_form.html",
        {"item": item, "frequencies": rec.FREQUENCIES, "accounts": active_accounts(db), "categories": active_categories(db)},
    )


@router.post("/recurring/{item_id}/edit", dependencies=[Depends(verify_csrf)])
def update_item(
    request: Request,
    item_id: int,
    name: str = Form(""),
    amount: str = Form(""),
    direction: str = Form("out"),
    frequency: str = Form("monthly"),
    next_due: str = Form(""),
    category_id: str = Form(""),
    account_id: str = Form(""),
    payee_match: str = Form(""),
    db: Session = Depends(get_db),
):
    item = db.get(RecurringItem, item_id)
    if item is None:
        raise HTTPException(404)
    try:
        _apply_form(item, name, amount, direction, frequency, next_due, category_id, account_id, payee_match)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/recurring/{item_id}/edit")
    if item.status == "detected":
        item.status = "confirmed"
    db.commit()
    flash(request, "Saved.")
    return redirect("/recurring")


@router.post("/recurring/{item_id}/{action}", dependencies=[Depends(verify_csrf)])
def item_action(request: Request, item_id: int, action: str, db: Session = Depends(get_db)):
    item = db.get(RecurringItem, item_id)
    if item is None:
        raise HTTPException(404)
    if action == "confirm":
        item.status = "confirmed"
    elif action == "dismiss":
        item.status = "dismissed"
    elif action == "restore":
        item.status = "confirmed"
    elif action == "accept-amount" and item.last_amount is not None:
        item.amount = item.last_amount
    elif action == "delete":
        db.delete(item)
    else:
        raise HTTPException(400)
    db.commit()
    return redirect("/recurring")


@router.post("/recurring-detect", dependencies=[Depends(verify_csrf)])
def detect(request: Request, db: Session = Depends(get_db)):
    rec.refresh_all(db, today())
    found = db.scalars(select(RecurringItem).where(RecurringItem.status == "detected")).all()
    db.commit()
    flash(request, f"{len(found)} detected item{'s' if len(found) != 1 else ''} waiting for review.")
    return redirect("/recurring")
