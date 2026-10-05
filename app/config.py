"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: str
    secret_key: str
    encryption_key: str
    app_username: str
    app_password: str
    cookie_secure: bool
    timezone: str
    sync_interval_hours: float
    scheduler_enabled: bool
    plaid_client_id: str
    plaid_secret: str
    plaid_env: str
    plaid_optional_products: tuple[str, ...]
    plaid_country_codes: tuple[str, ...]
    plaid_days_requested: int

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(",") if p.strip())


@lru_cache
def get_settings() -> Settings:
    data_dir = os.environ.get("DATA_DIR", "./data")
    return Settings(
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data_dir}/finance.db"),
        data_dir=data_dir,
        secret_key=os.environ.get("SECRET_KEY", ""),
        encryption_key=os.environ.get("ENCRYPTION_KEY", ""),
        app_username=os.environ.get("APP_USERNAME", "alex"),
        app_password=os.environ.get("APP_PASSWORD", ""),
        cookie_secure=_bool(os.environ.get("COOKIE_SECURE"), False),
        timezone=os.environ.get("APP_TIMEZONE", "America/Toronto"),
        sync_interval_hours=float(os.environ.get("SYNC_INTERVAL_HOURS", "6")),
        scheduler_enabled=_bool(os.environ.get("SCHEDULER_ENABLED"), True),
        plaid_client_id=os.environ.get("PLAID_CLIENT_ID", ""),
        plaid_secret=os.environ.get("PLAID_SECRET", ""),
        plaid_env=os.environ.get("PLAID_ENV", "production"),
        plaid_optional_products=_csv(os.environ.get("PLAID_OPTIONAL_PRODUCTS", "liabilities,investments")),
        plaid_country_codes=_csv(os.environ.get("PLAID_COUNTRY_CODES", "CA")),
        plaid_days_requested=int(os.environ.get("PLAID_DAYS_REQUESTED", "730")),
    )


def encryption_key_valid(key: str) -> bool:
    from cryptography.fernet import Fernet

    try:
        Fernet(key.encode())
    except (ValueError, TypeError):
        return False
    return True


def validate_settings(settings: Settings) -> list[str]:
    """Return a list of human-readable problems that prevent the app from starting."""
    problems = []
    if len(settings.secret_key) < 32:
        problems.append("SECRET_KEY must be set to a random string of at least 32 characters.")
    if not settings.encryption_key:
        problems.append("ENCRYPTION_KEY must be set (generate one with the command in .env.example).")
    elif not encryption_key_valid(settings.encryption_key):
        problems.append(
            f"ENCRYPTION_KEY is not a valid key (it is {len(settings.encryption_key)} characters; a valid key is 44 "
            "characters ending in '='). Generate a new one with the command in .env.example."
        )
    if not settings.app_password:
        problems.append("APP_PASSWORD must be set.")
    return problems
