from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Account, Category, Transaction
from ..money import MoneyError, parse_cents
from ..security import verify_csrf
from ..services import categorize, pipeline, transfers
from ..services.dates import month_bounds, parse_month
from ..services.payees import clean_payee
from .common import active_accounts, active_categories, flash, redirect, render, safe_next

router = APIRouter()
PAGE_SIZE = 200


def _parse_date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/transactions")
def list_transactions(
    request: Request,
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
    account: str | None = None,
    category: str | None = None,
    q: str | None = None,
    min_amount: str | None = None,
    max_amount: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = PAGE_SIZE,
    db: Session = Depends(get_db),
):
    start_d, end_d = _parse_date(start), _parse_date(end)
    use_range = bool(start_d or end_d)
    month = parse_month(month)
    if not use_range:
        start_d, end_d = month_bounds(month)

    query = select(Transaction).join(Account)
    if start_d:
        query = query.where(Transaction.date >= start_d)
    if end_d:
        query = query.where(Transaction.date <= end_d)
    if account and account.isdigit():
        query = query.where(Transaction.account_id == int(account))
    if category == "none":
        query = query.where(Transaction.category_id.is_(None), Transaction.is_transfer.is_(False))
    elif category == "transfer":
        query = query.where(Transaction.is_transfer.is_(True))
    elif category and category.isdigit():
        query = query.where(Transaction.category_id == int(category))
    if status in {"suggested", "confirmed"}:
        query = query.where(Transaction.category_status == status)
    if kind == "out":
        query = query.where(Transaction.amount < 0)
    elif kind == "in":
        query = query.where(Transaction.amount > 0)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.where(
            or_(
                func.lower(Transaction.payee_clean).like(like),
                func.lower(Transaction.payee_raw).like(like),
                func.lower(func.coalesce(Transaction.note, "")).like(like),
            )
        )
    try:
        if min_amount:
            query = query.where(func.abs(Transaction.amount) >= abs(parse_cents(min_amount)))
        if max_amount:
            query = query.where(func.abs(Transaction.amount) <= abs(parse_cents(max_amount)))
    except MoneyError:
        pass

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    limit = max(PAGE_SIZE, min(limit, 5000))
    txns = db.scalars(query.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(limit)).all()
    net = sum(t.amount for t in txns if not t.is_transfer)
    filters = {
        "start": start or "",
        "end": end or "",
        "account": account or "",
        "category": category or "",
        "q": q or "",
        "min_amount": min_amount or "",
        "max_amount": max_amount or "",
        "status": status or "",
        "kind": kind or "",
    }
    return render(
        request,
        db,
        "transactions.html",
        {
            "txns": txns,
            "total": total,
            "limit": limit,
            "net": net,
            "month": month,
            "use_range": use_range,
            "filters": filters,
            "filtered": any(v for k, v in filters.items() if k not in {"start", "end"}),
            "accounts": active_accounts(db),
            "categories": active_categories(db),
            "return_to": str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
        },
    )


def _form_context(db: Session, txn: Transaction | None, error: str | None = None, values: dict | None = None):
    return {
        "txn": txn,
        "accounts": active_accounts(db),
        "categories": active_categories(db),
        "error": error,
        "values": values or {},
    }


@router.get("/transactions/new")
def new_transaction(request: Request, account: int | None = None, db: Session = Depends(get_db)):
    return render(request, db, "transaction_form.html", _form_context(db, None, values={"account_id": account}))


def _read_amount(amount: str, direction: str) -> int:
    cents = abs(parse_cents(amount))
    if cents == 0:
        raise MoneyError("zero")
    return cents if direction == "in" else -cents


@router.post("/transactions/new", dependencies=[Depends(verify_csrf)])
def create_transaction(
    request: Request,
    date: str = Form(...),
    account_id: int = Form(...),
    amount: str = Form(...),
    direction: str = Form("out"),
    payee: str = Form(""),
    category_id: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    values = dict(date=date, account_id=account_id, amount=amount, direction=direction, payee=payee, category_id=category_id, note=note)
    d = _parse_date(date)
    acct = db.get(Account, account_id)
    try:
        cents = _read_amount(amount, direction)
    except MoneyError:
        cents = None
    if d is None or acct is None or cents is None or not payee.strip():
        error = "Date, account, payee and a non-zero amount are required."
        return render(request, db, "transaction_form.html", _form_context(db, None, error, values), 400)
    txn = Transaction(
        account_id=acct.id,
        date=d,
        amount=cents,
        payee_raw=payee.strip()[:256],
        payee_clean=clean_payee(payee),
        note=note.strip() or None,
        source="manual",
    )
    if category_id.isdigit():
        txn.category_id, txn.category_status, txn.suggestion_source = int(category_id), "confirmed", "user"
    else:
        txn.category_status = "suggested"
    db.add(txn)
    db.flush()
    pipeline.after_ingest(db, [txn])
    db.commit()
    flash(request, "Transaction added.")
    return redirect("/transactions", month=f"{d.year:04d}-{d.month:02d}")


@router.get("/transactions/{txn_id}")
def edit_transaction(request: Request, txn_id: int, back: str | None = None, db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404)
    partner = db.get(Transaction, txn.transfer_pair_id) if txn.transfer_pair_id else None
    ctx = _form_context(db, txn)
    ctx.update(back=safe_next(back, "/transactions"), partner=partner)
    return render(request, db, "transaction_form.html", ctx)


@router.post("/transactions/{txn_id}", dependencies=[Depends(verify_csrf)])
def update_transaction(
    request: Request,
    txn_id: int,
    date: str = Form(""),
    account_id: str = Form(""),
    amount: str = Form(""),
    direction: str = Form("out"),
    payee: str = Form(""),
    category_id: str = Form(""),
    note: str = Form(""),
    is_transfer: str = Form(""),
    always: str = Form(""),
    back: str = Form("/transactions"),
    db: Session = Depends(get_db),
):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404)
    if txn.source != "plaid":  # bank-synced core fields come from the bank
        d = _parse_date(date)
        acct = db.get(Account, int(account_id)) if account_id.isdigit() else None
        try:
            cents = _read_amount(amount, direction)
        except MoneyError:
            cents = None
        if d is None or acct is None or cents is None:
            ctx = _form_context(db, txn, "Date, account and a non-zero amount are required.")
            ctx["back"] = safe_next(back, "/transactions")
            return render(request, db, "transaction_form.html", ctx, 400)
        txn.date, txn.account_id, txn.amount = d, acct.id, cents
        if payee.strip():
            txn.payee_raw = payee.strip()[:256]
    if payee.strip():
        txn.payee_clean = payee.strip()[:256]
    txn.note = note.strip() or None
    new_cat = int(category_id) if category_id.isdigit() else None
    categorize.confirm(txn, new_cat)
    want_transfer = bool(is_transfer)
    if want_transfer != txn.is_transfer:
        transfers.set_transfer(db, txn, want_transfer)
    if always and new_cat is not None:
        categorize.create_rule_for_payee(db, txn.payee_clean, new_cat)
    pipeline.after_edit(db)
    db.commit()
    flash(request, "Saved.")
    return redirect(safe_next(back, "/transactions"))


@router.post("/transactions/{txn_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_transaction(request: Request, txn_id: int, back: str = Form("/transactions"), db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404)
    if txn.source == "plaid":
        flash(request, "Bank-synced transactions can't be deleted; mark it as a transfer or recategorize it instead.", "error")
        return redirect(f"/transactions/{txn_id}")
    transfers.unlink_partner(db, txn)
    db.delete(txn)
    pipeline.after_edit(db)
    db.commit()
    flash(request, "Transaction deleted.")
    return redirect(safe_next(back, "/transactions"))


# --- JSON endpoints used for inline editing ------------------------------------------


@router.post("/api/transactions/{txn_id}/category", dependencies=[Depends(verify_csrf)])
def api_set_category(txn_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404)
    raw = payload.get("category_id")
    category = db.get(Category, int(raw)) if str(raw or "").isdigit() else None
    categorize.confirm(txn, category.id if category else None)
    rule_created = False
    if payload.get("always") and category is not None and txn.payee_clean:
        categorize.create_rule_for_payee(db, txn.payee_clean, category.id)
        rule_created = True
    pipeline.after_edit(db)
    db.commit()
    return JSONResponse(
        {
            "ok": True,
            "category": category.name if category else None,
            "payee": txn.payee_clean,
            "rule_created": rule_created,
        }
    )


@router.post("/api/transactions/{txn_id}/transfer", dependencies=[Depends(verify_csrf)])
def api_set_transfer(txn_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404)
    transfers.set_transfer(db, txn, bool(payload.get("is_transfer")))
    pipeline.after_edit(db)
    db.commit()
    return JSONResponse({"ok": True, "is_transfer": txn.is_transfer})
