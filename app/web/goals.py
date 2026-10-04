from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Account, Goal, GoalContribution
from ..money import MoneyError, parse_cents
from ..security import verify_csrf
from ..services.dates import today
from ..services.goals import goal_progress
from .common import active_accounts, flash, redirect, render

router = APIRouter()


@router.get("/goals")
def goals_page(request: Request, db: Session = Depends(get_db)):
    now = today()
    goals = db.scalars(select(Goal).order_by(Goal.type.desc(), Goal.created_at)).all()
    rows = [(g, goal_progress(db, g, now)) for g in goals]
    return render(request, db, "goals.html", {"rows": rows, "accounts": active_accounts(db), "goal": None})


async def _apply_form(request: Request, db: Session, goal: Goal) -> str | None:
    form = await request.form()
    name = str(form.get("name", "")).strip()
    gtype = form.get("type", "savings")
    if not name or gtype not in {"savings", "debt"}:
        return "Name and type are required."
    try:
        target = abs(parse_cents(str(form.get("target_amount", ""))))
    except MoneyError:
        return "Enter a target amount."
    if target == 0:
        return "Target must be more than zero."
    try:
        target_date = dt.date.fromisoformat(str(form.get("target_date"))) if form.get("target_date") else None
    except ValueError:
        return "Target date is not a valid date."
    ids = [int(v) for v in form.getlist("account_ids") if str(v).isdigit()]
    accounts = list(db.scalars(select(Account).where(Account.id.in_(ids)))) if ids else []
    source = form.get("progress_source", "accounts")
    if gtype == "debt" and not accounts:
        return "Link the loan or credit card account this goal pays off."
    if gtype == "savings" and source == "accounts" and not accounts:
        return "Link at least one account, or track progress with manual contributions."
    goal.name, goal.type, goal.target_amount, goal.target_date = name[:128], gtype, target, target_date
    goal.progress_source = "accounts" if gtype == "debt" else source if source in {"accounts", "contributions"} else "accounts"
    goal.accounts = accounts
    return None


@router.post("/goals", dependencies=[Depends(verify_csrf)])
async def create_goal(request: Request, db: Session = Depends(get_db)):
    goal = Goal()
    error = await _apply_form(request, db, goal)
    if error:
        flash(request, error, "error")
        return redirect("/goals")
    db.add(goal)
    db.commit()
    flash(request, f"Goal “{goal.name}” added.")
    return redirect("/goals")


@router.get("/goals/{goal_id}/edit")
def edit_goal(request: Request, goal_id: int, db: Session = Depends(get_db)):
    goal = db.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(404)
    return render(request, db, "goal_form.html", {"goal": goal, "accounts": active_accounts(db)})


@router.post("/goals/{goal_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_goal(request: Request, goal_id: int, db: Session = Depends(get_db)):
    goal = db.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(404)
    error = await _apply_form(request, db, goal)
    if error:
        db.rollback()
        flash(request, error, "error")
        return redirect(f"/goals/{goal_id}/edit")
    db.commit()
    flash(request, "Saved.")
    return redirect("/goals")


@router.post("/goals/{goal_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_goal(request: Request, goal_id: int, db: Session = Depends(get_db)):
    goal = db.get(Goal, goal_id)
    if goal is not None:
        db.delete(goal)
        db.commit()
    return redirect("/goals")


@router.post("/goals/{goal_id}/contributions", dependencies=[Depends(verify_csrf)])
async def add_contribution(request: Request, goal_id: int, db: Session = Depends(get_db)):
    goal = db.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(404)
    form = await request.form()
    try:
        amount = parse_cents(str(form.get("amount", "")))
        date = dt.date.fromisoformat(str(form.get("date"))) if form.get("date") else today()
    except (MoneyError, ValueError):
        flash(request, "Enter an amount (negative for a withdrawal) and a valid date.", "error")
        return redirect("/goals")
    db.add(GoalContribution(goal_id=goal.id, amount=amount, date=date, note=str(form.get("note", ""))[:256] or None))
    db.commit()
    return redirect("/goals")


@router.post("/goals/contributions/{cid}/delete", dependencies=[Depends(verify_csrf)])
def delete_contribution(cid: int, db: Session = Depends(get_db)):
    c = db.get(GoalContribution, cid)
    if c is not None:
        db.delete(c)
        db.commit()
    return redirect("/goals")
