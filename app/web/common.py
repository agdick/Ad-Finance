from __future__ import annotations

import datetime as dt
import os
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Category, Transaction
from ..money import cents_to_input, format_cents
from ..security import csrf_token
from ..services import alerts
from ..services.dates import month_label, shift_month, today

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"))
templates.env.filters["money"] = format_cents
templates.env.filters["money_signed"] = lambda c: format_cents(c, signed=True)
templates.env.filters["money_input"] = cents_to_input
templates.env.filters["month_label"] = month_label
templates.env.globals["shift_month"] = shift_month


def _short_date(d: dt.date | None) -> str:
    if d is None:
        return ""
    return d.strftime("%b %-d") if d.year == today().year else d.strftime("%b %-d, %Y")


def _ago(ts: dt.datetime | None) -> str:
    if ts is None:
        return "never"
    secs = (dt.datetime.utcnow() - ts).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} d ago"


templates.env.filters["short_date"] = _short_date
templates.env.filters["ago"] = _ago


def review_count(db: Session) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.category_status == "suggested", Transaction.is_transfer.is_(False))
    ) or 0


def render(request: Request, db: Session, name: str, ctx: dict | None = None, status_code: int = 200):
    context = {
        "request": request,
        "csrf": csrf_token(request),
        "nav_review_count": review_count(db),
        "nav_alert_count": len(alerts.active_alerts(db)),
        "flash": request.session.pop("flash", None),
        "today": today(),
    }
    context.update(ctx or {})
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def redirect(url: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def active_categories(db: Session) -> list[Category]:
    return list(
        db.scalars(select(Category).where(Category.archived.is_(False)).order_by(Category.kind.desc(), Category.sort_order, Category.name))
    )


def active_accounts(db: Session) -> list[Account]:
    return list(db.scalars(select(Account).where(Account.archived.is_(False)).order_by(Account.type, Account.name)))


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow local redirects."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return default
