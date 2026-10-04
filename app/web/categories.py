from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Category, PayeeRule
from ..money import MoneyError, parse_cents
from ..security import verify_csrf
from ..services import categorize, pipeline
from ..services.settings_store import get_setting, parse_thresholds
from .common import active_categories, flash, redirect, render

router = APIRouter()


@router.get("/categories")
def categories_page(request: Request, db: Session = Depends(get_db)):
    cats = db.scalars(select(Category).order_by(Category.archived, Category.kind.desc(), Category.sort_order, Category.name)).all()
    rules = db.scalars(select(PayeeRule).order_by(PayeeRule.match_value)).all()
    return render(
        request,
        db,
        "categories.html",
        {
            "cats": cats,
            "rules": rules,
            "active": active_categories(db),
            "default_thresholds": get_setting(db, "alert_thresholds"),
            "total_limit": sum(c.monthly_limit or 0 for c in cats if c.kind == "expense" and not c.archived),
        },
    )


def _limit(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    return abs(parse_cents(value))


@router.post("/categories", dependencies=[Depends(verify_csrf)])
def create_category(
    request: Request,
    name: str = Form(""),
    kind: str = Form("expense"),
    monthly_limit: str = Form(""),
    db: Session = Depends(get_db),
):
    name = name.strip()[:64]
    if not name:
        flash(request, "Enter a name.", "error")
        return redirect("/categories")
    if db.scalar(select(Category).where(func.lower(Category.name) == name.lower())):
        flash(request, f"A category called “{name}” already exists.", "error")
        return redirect("/categories")
    try:
        limit = _limit(monthly_limit)
    except MoneyError:
        flash(request, "Limit must be an amount, e.g. 450", "error")
        return redirect("/categories")
    max_order = db.scalar(select(func.max(Category.sort_order))) or 0
    db.add(Category(name=name, kind="income" if kind == "income" else "expense", monthly_limit=limit, sort_order=max_order + 1))
    db.commit()
    flash(request, f"Added “{name}”.")
    return redirect("/categories")


@router.post("/categories/{cat_id}", dependencies=[Depends(verify_csrf)])
def update_category(
    request: Request,
    cat_id: int,
    name: str = Form(""),
    kind: str = Form("expense"),
    monthly_limit: str = Form(""),
    alert_thresholds: str = Form(""),
    db: Session = Depends(get_db),
):
    cat = db.get(Category, cat_id)
    if cat is None:
        raise HTTPException(404)
    try:
        cat.monthly_limit = _limit(monthly_limit)
    except MoneyError:
        flash(request, "Limit must be an amount, e.g. 450", "error")
        return redirect("/categories")
    if name.strip():
        cat.name = name.strip()[:64]
    cat.kind = "income" if kind == "income" else "expense"
    thresholds = parse_thresholds(alert_thresholds)
    cat.alert_thresholds = ",".join(map(str, thresholds)) if thresholds else None
    try:
        pipeline.after_edit(db)
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "Another category already has that name.", "error")
        return redirect("/categories")
    flash(request, f"Saved “{cat.name}”.")
    return redirect("/categories")


@router.post("/categories/{cat_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_category(request: Request, cat_id: int, db: Session = Depends(get_db)):
    cat = db.get(Category, cat_id)
    if cat is None:
        raise HTTPException(404)
    cat.archived = not cat.archived
    db.commit()
    flash(request, f"“{cat.name}” {'archived' if cat.archived else 'restored'}.")
    return redirect("/categories")


@router.post("/rules", dependencies=[Depends(verify_csrf)])
def create_rule(
    request: Request,
    match_type: str = Form("contains"),
    match_value: str = Form(""),
    category_id: str = Form(""),
    mark_transfer: str = Form(""),
    db: Session = Depends(get_db),
):
    if not match_value.strip() or (not category_id.isdigit() and not mark_transfer):
        flash(request, "A rule needs text to match and a category (or “mark as transfer”).", "error")
        return redirect("/categories#rules")
    db.add(
        PayeeRule(
            match_type=match_type if match_type in {"exact", "contains", "starts_with"} else "contains",
            match_value=match_value.strip()[:256],
            category_id=int(category_id) if category_id.isdigit() else None,
            mark_transfer=bool(mark_transfer),
        )
    )
    db.flush()
    n = categorize.resuggest_unconfirmed(db)
    pipeline.after_edit(db)
    db.commit()
    flash(request, f"Rule added; re-checked {n} unconfirmed transaction{'s' if n != 1 else ''}.")
    return redirect("/categories#rules")


@router.post("/rules/{rule_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_rule(request: Request, rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(PayeeRule, rule_id)
    if rule is not None:
        db.delete(rule)
        db.commit()
    return redirect("/categories#rules")
