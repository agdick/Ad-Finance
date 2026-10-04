from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import check_login, verify_csrf
from .common import redirect, render, safe_next

router = APIRouter()
_failures: list[float] = []


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if request.session.get("user"):
        return redirect(safe_next(next))
    return render(request, db, "login.html", {"next": safe_next(next), "error": None})


@router.post("/login", dependencies=[Depends(verify_csrf)])
async def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    now = time.monotonic()
    _failures[:] = [t for t in _failures if now - t < 900]
    if len(_failures) >= 5:
        await asyncio.sleep(min(2 ** (len(_failures) - 5), 30))  # slow down guessing
    if check_login(username, password):
        _failures.clear()
        csrf = request.session.get("csrf")
        request.session.clear()
        request.session.update({"user": username.strip(), "csrf": csrf})
        return redirect(safe_next(next))
    _failures.append(now)
    return render(request, db, "login.html", {"next": safe_next(next), "error": "Wrong username or password."}, 401)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return redirect("/login")
