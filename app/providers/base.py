"""The SyncProvider interface.

A provider is a *data feed*: it never touches the database. `services.sync` turns what
providers return into rows in the app's own tables, and budget logic, categorization and
the UI only ever read those tables. Adding another aggregator means implementing this
class, registering it in `providers/__init__.py`, and (if its link flow needs a browser
widget) adding a small handler in `static/app.js`.

Amounts use the app's convention: integer cents, negative = money leaving the account.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal


class ProviderError(RuntimeError):
    """A provider call failed. `message` must be safe to show and log (no secrets)."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class ProviderAuthError(ProviderError):
    """The connection needs the user to sign in again (status: login_required)."""


@dataclass
class LinkResult:
    credentials: dict  # stored encrypted; e.g. {"access_token": ...}
    external_id: str  # stable id of the connection at the provider (Plaid item_id)
    institution_name: str | None = None


@dataclass
class ProviderAccount:
    external_id: str
    name: str
    type: str  # one of models.ACCOUNT_TYPES
    subtype: str | None = None
    mask: str | None = None
    institution: str | None = None


@dataclass
class ProviderBalance:
    account_external_id: str
    current: int | None  # cents; for credit/loan, positive = owed
    available: int | None = None


@dataclass
class ProviderTransaction:
    external_id: str
    account_external_id: str
    date: dt.date
    amount: int  # cents, negative = outflow
    payee: str
    category: str | None = None  # provider's own category label
    is_pending: bool = False
    pending_external_id: str | None = None  # posted txn: id of the pending txn it replaces


@dataclass
class TransactionPage:
    added: list[ProviderTransaction] = field(default_factory=list)
    modified: list[ProviderTransaction] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # external ids
    next_cursor: str | None = None


@dataclass
class ProviderHolding:
    account_external_id: str
    name: str
    ticker: str | None
    quantity: Decimal | None
    value: int | None  # cents


@dataclass
class ProviderLiability:
    account_external_id: str
    interest_rate: Decimal | None = None  # percent, e.g. 19.99
    min_payment: int | None = None
    next_payment_due: dt.date | None = None


@dataclass
class ConnectionHealth:
    status: str  # ok | login_required | pending_expiration | error
    detail: str | None = None


class SyncProvider(ABC):
    name: str = ""
    display_name: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """True when the provider has the API credentials it needs (from the environment)."""

    @abstractmethod
    def link(self, existing_credentials: dict | None = None) -> dict:
        """Start linking (or re-linking, when existing_credentials is given).

        Returns a JSON-safe dict for the browser, e.g. {"kind": "plaid_link", "link_token": ...}.
        """

    @abstractmethod
    def complete_link(self, payload: dict) -> LinkResult:
        """Finish linking with what the browser widget returned."""

    @abstractmethod
    def fetch_accounts(self, credentials: dict) -> list[ProviderAccount]: ...

    @abstractmethod
    def fetch_transactions(self, credentials: dict, since: str | None) -> TransactionPage:
        """Return all changes after the opaque cursor `since` (None = from the beginning)."""

    @abstractmethod
    def fetch_balances(self, credentials: dict) -> list[ProviderBalance]: ...

    def fetch_holdings(self, credentials: dict) -> list[ProviderHolding]:
        return []

    def fetch_liabilities(self, credentials: dict) -> list[ProviderLiability]:
        return []

    def check_status(self, credentials: dict) -> ConnectionHealth:
        return ConnectionHealth("ok")

    def disconnect(self, credentials: dict) -> None:
        return None
