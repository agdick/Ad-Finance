from __future__ import annotations

import pytest

PAGES = ["/", "/transactions", "/review", "/recurring", "/goals", "/accounts", "/accounts/new",
         "/import", "/categories", "/settings", "/transactions/new", "/?month=2025-02"]


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/api/transactions/1/category").status_code in (401, 405)


def test_wrong_password(client):
    from tests.conftest import csrf_from

    token = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"username": "alex", "password": "nope", "csrf_token": token})
    assert r.status_code == 401


def test_post_without_csrf_rejected(auth_client):
    r = auth_client.post("/categories", data={"name": "X"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(auth_client, path):
    r = auth_client.get(path)
    assert r.status_code == 200, r.text[:2000]


@pytest.mark.parametrize("key", ["not-a-key", "abc123" * 7, "x" * 44])
def test_invalid_encryption_key_rejected_at_startup(key):
    import dataclasses

    from app.config import get_settings, validate_settings

    problems = validate_settings(dataclasses.replace(get_settings(), encryption_key=key))
    assert any("ENCRYPTION_KEY is not a valid key" in p for p in problems)


def test_valid_encryption_key_accepted():
    from app.config import get_settings, validate_settings

    assert validate_settings(get_settings()) == []
