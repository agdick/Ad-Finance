"""Encryption at rest for provider credentials, single-user login, and CSRF tokens."""
from __future__ import annotations

import hmac
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from .config import get_settings


class CredentialError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialError("ENCRYPTION_KEY in .env is not a valid key; generate a new one (see .env.example) and restart.") from exc


def encrypt_credentials(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_credentials(token: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as exc:
        raise CredentialError("Stored credentials could not be decrypted (was ENCRYPTION_KEY changed?)") from exc


def check_login(username: str, password: str) -> bool:
    s = get_settings()
    user_ok = hmac.compare_digest(username.strip().encode(), s.app_username.encode())
    pass_ok = hmac.compare_digest(password.encode(), s.app_password.encode())
    return bool(s.app_password) and user_ok and pass_ok


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency for state-changing routes. Accepts a form field or X-CSRF-Token header."""
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if "form" in content_type:
            form = await request.form()
            supplied = form.get("csrf_token")
    if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and try again.")
