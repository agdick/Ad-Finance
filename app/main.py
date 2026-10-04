from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .config import get_settings, validate_settings
from .db import SessionLocal
from .services.seed import seed_categories
from .web import accounts, auth, categories, goals, home, imports, recurring, review, settings, transactions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full URLs at INFO; keep third-party request logging quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

PUBLIC_PATHS = ("/login", "/static/", "/healthz")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db = SessionLocal()
    try:
        seed_categories(db)
    finally:
        db.close()
    scheduler.start()
    yield
    scheduler.stop()


def create_app() -> FastAPI:
    s = get_settings()
    problems = validate_settings(s)
    if problems:
        for p in problems:
            print(f"Configuration error: {p}", file=sys.stderr)
        raise SystemExit(1)

    app = FastAPI(title="Finance", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if not path.startswith(PUBLIC_PATHS) and not request.session.get("user"):
            if path.startswith("/api/"):
                return PlainTextResponse("Not signed in", status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=303)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # Added after the auth middleware so it wraps it (sessions are available inside).
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        session_cookie="finance_session",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=s.cookie_secure,
    )

    app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    for module in (auth, home, transactions, review, recurring, goals, accounts, imports, categories, settings):
        app.include_router(module.router)
    return app
