from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Transaction
from ..security import verify_csrf
from ..services import categorize, pipeline
from .common import active_categories, flash, redirect, render

router = APIRouter()


@router.get("/review")
def review_queue(request: Request, db: Session = Depends(get_db)):
    txns = db.scalars(
        select(Transaction)
        .where(Transaction.category_status == "suggested", Transaction.is_transfer.is_(False))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(500)
    ).all()
    return render(request, db, "review.html", {"txns": txns, "categories": active_categories(db)})


@router.post("/review", dependencies=[Depends(verify_csrf)])
async def review_confirm(request: Request, db: Session = Depends(get_db)):
    """Bulk confirm. Form fields: select=<id> (checked rows), cat_<id>=<category id>,
    always_<id>=1 to create a payee rule. action=all confirms every row on the page."""
    form = await request.form()
    action = form.get("action", "selected")
    ids = {int(k[4:]) for k in form if k.startswith("cat_") and k[4:].isdigit()}
    if action != "all":
        ids &= {int(v) for v in form.getlist("select") if str(v).isdigit()}
    confirmed = rules = 0
    for txn_id in sorted(ids):
        txn = db.get(Transaction, txn_id)
        if txn is None or txn.category_status == "confirmed":
            continue
        raw = form.get(f"cat_{txn_id}", "")
        cat_id = int(raw) if str(raw).isdigit() else None
        if cat_id is None:
            continue  # nothing to confirm yet; leave it in the queue
        categorize.confirm(txn, cat_id)
        confirmed += 1
        if form.get(f"always_{txn_id}") and txn.payee_clean:
            db.flush()
            categorize.create_rule_for_payee(db, txn.payee_clean, cat_id)
            rules += 1
    pipeline.after_edit(db)
    db.commit()
    msg = f"Confirmed {confirmed} transaction{'s' if confirmed != 1 else ''}."
    if rules:
        msg += f" Created {rules} rule{'s' if rules != 1 else ''}."
    flash(request, msg)
    return redirect("/review")
