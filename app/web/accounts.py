from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ACCOUNT_TYPES, Account, ProviderConnection
from ..money import MoneyError, parse_cents
from ..providers import all_providers, get_provider
from ..providers.base import ProviderError
from ..security import CredentialError, verify_csrf
from ..services import sync as sync_service
from ..services.balances import record_balance
from ..services.dates import today
from .common import flash, redirect, render

router = APIRouter()
log = logging.getLogger(__name__)
TYPE_ORDER = list(ACCOUNT_TYPES)


@router.get("/accounts")
def accounts_page(request: Request, show_archived: int = 0, db: Session = Depends(get_db)):
    q = select(Account)
    if not show_archived:
        q = q.where(Account.archived.is_(False))
    accounts = db.scalars(q.order_by(Account.name)).all()
    groups = []
    for t in TYPE_ORDER:
        members = [a for a in accounts if a.type == t]
        if members:
            subtotal = sum(a.current_balance or 0 for a in members)
            groups.append({"type": t, "label": ACCOUNT_TYPES[t], "accounts": members, "subtotal": subtotal, "liability": t in {"credit", "loan"}})
    assets = sum(a.current_balance or 0 for a in accounts if not a.is_liability and not a.archived)
    debts = sum(a.current_balance or 0 for a in accounts if a.is_liability and not a.archived)
    connections = db.scalars(select(ProviderConnection).where(ProviderConnection.status != "disconnected")).all()
    return render(
        request,
        db,
        "accounts.html",
        {
            "groups": groups,
            "assets": assets,
            "debts": debts,
            "connections": connections,
            "providers": [p for p in all_providers() if p.is_configured()],
            "show_archived": show_archived,
        },
    )


def _form_ctx(account: Account | None, error: str | None = None):
    return {"account": account, "types": ACCOUNT_TYPES, "error": error}


@router.get("/accounts/new")
def new_account(request: Request, db: Session = Depends(get_db)):
    return render(request, db, "account_form.html", _form_ctx(None))


def _apply(account: Account, name, institution, type_, subtype, balance, interest_rate) -> str | None:
    if not name.strip() or type_ not in ACCOUNT_TYPES:
        return "Name and type are required."
    account.name = name.strip()[:128]
    account.institution = institution.strip()[:128] or None
    if account.source != "plaid":
        account.type = type_
    account.subtype = subtype.strip()[:64] or None
    if interest_rate.strip():
        try:
            account.interest_rate = Decimal(interest_rate.strip().rstrip("%"))
        except InvalidOperation:
            return "Interest rate must be a number, e.g. 6.45"
    elif account.source != "plaid":
        account.interest_rate = None
    return None


@router.post("/accounts/new", dependencies=[Depends(verify_csrf)])
def create_account(
    request: Request,
    name: str = Form(""),
    institution: str = Form(""),
    type: str = Form("chequing"),
    subtype: str = Form(""),
    balance: str = Form(""),
    interest_rate: str = Form(""),
    source: str = Form("manual"),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    account = Account(source="csv" if source == "csv" else "manual")
    error = _apply(account, name, institution, type, subtype, balance, interest_rate)
    if error:
        return render(request, db, "account_form.html", _form_ctx(None, error), 400)
    db.add(account)
    db.flush()
    if balance.strip():
        try:
            record_balance(db, account, parse_cents(balance), today())
        except MoneyError:
            pass
    db.commit()
    flash(request, f"Account “{account.name}” created.")
    if next == "import":
        return redirect("/import", account=account.id)
    return redirect("/accounts")


@router.get("/accounts/{account_id}/edit")
def edit_account(request: Request, account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404)
    return render(request, db, "account_form.html", _form_ctx(account))


@router.post("/accounts/{account_id}/edit", dependencies=[Depends(verify_csrf)])
def update_account(
    request: Request,
    account_id: int,
    name: str = Form(""),
    institution: str = Form(""),
    type: str = Form("chequing"),
    subtype: str = Form(""),
    balance: str = Form(""),
    interest_rate: str = Form(""),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404)
    error = _apply(account, name, institution, type, subtype, balance, interest_rate)
    if error:
        return render(request, db, "account_form.html", _form_ctx(account, error), 400)
    if account.source != "plaid" and balance.strip():
        try:
            new_balance = parse_cents(balance)
            if new_balance != account.current_balance:
                record_balance(db, account, new_balance, today())
        except MoneyError:
            pass
    db.commit()
    flash(request, "Saved.")
    return redirect("/accounts")


@router.post("/accounts/{account_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_account(request: Request, account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404)
    account.archived = not account.archived
    db.commit()
    flash(request, f"“{account.name}” {'archived' if account.archived else 'restored'}.")
    return redirect("/accounts")


# --- bank connections (provider-agnostic) ------------------------------------------


@router.post("/api/connections/link", dependencies=[Depends(verify_csrf)])
def link_start(payload: dict = Body(...), db: Session = Depends(get_db)):
    provider = payload.get("provider", "plaid")
    connection_id = payload.get("connection_id")
    try:
        get_provider(provider)
        config = sync_service.start_link(db, provider, int(connection_id) if connection_id else None)
    except (KeyError, ProviderError, CredentialError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **config})


LINK_EVENT_FIELDS = (
    "event_name",
    "view_name",
    "error_type",
    "error_code",
    "error_message",
    "exit_status",
    "institution_name",
    "institution_id",
    "link_session_id",
    "request_id",
)


@router.post("/api/connections/link-event", dependencies=[Depends(verify_csrf)])
def link_event(payload: dict = Body(...)):
    """Log a step of the bank-linking widget. Only whitelisted, non-secret fields are kept."""
    parts = []
    for key in LINK_EVENT_FIELDS:
        value = payload.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={str(value)[:200]!r}")
    log.info("Link event: %s", " ".join(parts) or "(empty)")
    return JSONResponse({"ok": True})


@router.post("/api/connections/complete", dependencies=[Depends(verify_csrf)])
def link_complete(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    provider = payload.get("provider", "plaid")
    connection_id = payload.get("connection_id")
    try:
        if connection_id:  # update-mode re-link: same credentials, now working again
            conn = db.get(ProviderConnection, int(connection_id))
            if conn is None:
                raise ProviderError("Unknown connection.")
            sync_service.mark_relinked(db, conn)
        else:
            conn = sync_service.complete_link(db, provider, payload.get("data") or {})
    except (KeyError, ProviderError, CredentialError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    report = sync_service.sync_connection(db, conn)
    msg = f"Connected {conn.institution_name or 'bank'}." if report.ok else f"Connected, but the first sync failed: {report.message}"
    flash(request, msg, "ok" if report.ok else "error")
    return JSONResponse({"ok": True})


@router.post("/connections/{connection_id}/disconnect", dependencies=[Depends(verify_csrf)])
def disconnect(request: Request, connection_id: int, db: Session = Depends(get_db)):
    conn = db.get(ProviderConnection, connection_id)
    if conn is None:
        raise HTTPException(404)
    sync_service.disconnect(db, conn)
    flash(request, "Disconnected. Accounts and transaction history were kept.")
    return redirect("/accounts")


@router.post("/sync", dependencies=[Depends(verify_csrf)])
def sync_now(request: Request, next: str = Form("/accounts"), db: Session = Depends(get_db)):
    reports = sync_service.sync_all(db)
    if not reports:
        flash(request, "Nothing to sync (no bank connections, or a sync is already running).")
    else:
        failed = [r for r in reports if not r.ok]
        added = sum(r.added for r in reports)
        if failed:
            flash(request, f"Synced with problems: {failed[0].message}", "error")
        else:
            flash(request, f"Synced. {added} new transaction{'s' if added != 1 else ''}.")
    return redirect(next if next.startswith("/") and not next.startswith("//") else "/accounts")
