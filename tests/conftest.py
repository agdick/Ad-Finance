from __future__ import annotations

import datetime as dt
import os
import re

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("APP_USERNAME", "alex")
os.environ.setdefault("APP_PASSWORD", "correct horse")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["PLAID_CLIENT_ID"] = ""
os.environ["PLAID_SECRET"] = ""


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    from app.config import get_settings
    from app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    yield tmp_path
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def db(db_env):
    from app.db import SessionLocal
    from app.services.seed import seed_categories

    session = SessionLocal()
    seed_categories(session)
    yield session
    session.close()


@pytest.fixture()
def today(monkeypatch):
    """Pin 'today' to a fixed date so month maths are deterministic."""
    fixed = dt.date(2026, 10, 20)
    import app.services.dates as dates

    monkeypatch.setattr(dates, "today", lambda: fixed)
    for mod in ("app.services.pipeline", "app.services.sync", "app.web.common", "app.web.home",
                "app.web.recurring", "app.web.goals", "app.web.accounts", "app.scheduler"):
        m = __import__(mod, fromlist=["today"])
        if hasattr(m, "today"):
            monkeypatch.setattr(m, "today", lambda: fixed)
    monkeypatch.setattr(dates, "current_month", lambda: "2026-10")
    import app.services.alerts as alerts
    import app.web.home as home

    monkeypatch.setattr(alerts, "current_month", lambda: "2026-10")
    monkeypatch.setattr(home, "current_month", lambda: "2026-10")
    return fixed


@pytest.fixture()
def client(db_env):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html) or re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


@pytest.fixture()
def auth_client(client):
    page = client.get("/login")
    token = csrf_from(page.text)
    r = client.post("/login", data={"username": "alex", "password": "correct horse", "csrf_token": token, "next": "/"})
    assert r.status_code == 200 and "This month" in r.text
    client.csrf = csrf_from(r.text)
    return client
