"""PlaidProvider: Plaid's REST API via httpx.

Uses /accounts/get for balances (cached balances, no per-call fee) rather than
/accounts/balance/get. Never logs request or response bodies, which carry secrets.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import httpx

from ..config import get_settings
from ..money import decimal_to_cents
from .base import (
    ConnectionHealth,
    LinkResult,
    ProviderAccount,
    ProviderAuthError,
    ProviderBalance,
    ProviderError,
    ProviderHolding,
    ProviderLiability,
    ProviderTransaction,
    SyncProvider,
    TransactionPage,
)

log = logging.getLogger(__name__)

HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# Error codes that mean the user must go through Link (update mode) again.
REAUTH_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "INVALID_CREDENTIALS",
    "INVALID_MFA",
    "ITEM_LOCKED",
    "USER_SETUP_REQUIRED",
    "ACCESS_NOT_GRANTED",
    "NO_ACCOUNTS",
}
# Errors meaning a product isn't available for this Item/plan; treated as "no data".
UNSUPPORTED_CODES = {
    "PRODUCTS_NOT_SUPPORTED",
    "PRODUCT_NOT_ENABLED",
    "INVALID_PRODUCT",
    "ADDITIONAL_CONSENT_REQUIRED",
    "NO_LIABILITY_ACCOUNTS",
    "NO_INVESTMENT_ACCOUNTS",
    "NO_INVESTMENT_AUTH_ACCOUNTS",
    "PRODUCT_NOT_READY",
}


def map_account_type(plaid_type: str | None, subtype: str | None) -> str:
    plaid_type = (plaid_type or "").lower()
    subtype = (subtype or "").lower()
    if plaid_type == "depository":
        return "chequing" if subtype in {"checking", "chequing", "prepaid", "paypal", "cash management", ""} else "savings"
    if plaid_type == "credit":
        return "credit"
    if plaid_type == "loan":
        return "loan"
    if plaid_type in {"investment", "brokerage"}:
        return "registered"
    return "chequing"


def _cents(value) -> int | None:
    if value is None:
        return None
    return decimal_to_cents(Decimal(str(value)))


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


class PlaidProvider(SyncProvider):
    name = "plaid"
    display_name = "Plaid"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport

    # -- plumbing -----------------------------------------------------------
    def is_configured(self) -> bool:
        return get_settings().plaid_configured

    def _post(self, path: str, body: dict) -> dict:
        s = get_settings()
        if not s.plaid_configured:
            raise ProviderError("Plaid is not configured (set PLAID_CLIENT_ID and PLAID_SECRET).")
        host = HOSTS.get(s.plaid_env)
        if host is None:
            raise ProviderError(f"Unknown PLAID_ENV {s.plaid_env!r}; use sandbox or production.")
        payload = {"client_id": s.plaid_client_id, "secret": s.plaid_secret, **body}
        try:
            with httpx.Client(base_url=host, timeout=60, transport=self._transport) as client:
                resp = client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach Plaid ({type(exc).__name__}).") from None
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"Plaid returned HTTP {resp.status_code} with a non-JSON body.") from None
        if resp.status_code >= 400 or "error_code" in data:
            code = data.get("error_code") or f"HTTP_{resp.status_code}"
            message = data.get("display_message") or data.get("error_message") or "Plaid request failed"
            log.warning("Plaid %s failed: %s", path, code)
            if code in REAUTH_CODES:
                raise ProviderAuthError(f"{code}: {message}", code=code)
            raise ProviderError(f"{code}: {message}", code=code)
        return data

    # -- linking ------------------------------------------------------------
    def link(self, existing_credentials: dict | None = None) -> dict:
        s = get_settings()
        body: dict = {
            "client_name": "Finance",
            "language": "en",
            "country_codes": list(s.plaid_country_codes),
            "user": {"client_user_id": "owner"},
        }
        if existing_credentials:
            body["access_token"] = existing_credentials["access_token"]  # update mode
        else:
            body["products"] = ["transactions"]
            body["transactions"] = {"days_requested": s.plaid_days_requested}
            if s.plaid_optional_products:
                body["optional_products"] = list(s.plaid_optional_products)
        data = self._post("/link/token/create", body)
        return {"kind": "plaid_link", "link_token": data["link_token"]}

    def complete_link(self, payload: dict) -> LinkResult:
        public_token = payload.get("public_token")
        if not public_token:
            raise ProviderError("Missing public_token from Plaid Link.")
        data = self._post("/item/public_token/exchange", {"public_token": public_token})
        institution = (payload.get("metadata") or {}).get("institution") or {}
        return LinkResult(
            credentials={"access_token": data["access_token"]},
            external_id=data["item_id"],
            institution_name=institution.get("name"),
        )

    # -- data ---------------------------------------------------------------
    def _accounts_raw(self, credentials: dict) -> list[dict]:
        return self._post("/accounts/get", {"access_token": credentials["access_token"]})["accounts"]

    def fetch_accounts(self, credentials: dict) -> list[ProviderAccount]:
        out = []
        for a in self._accounts_raw(credentials):
            subtype = a.get("subtype") or None
            if subtype and a.get("type") == "investment":
                subtype = subtype.upper()  # "tfsa" -> "TFSA"
            out.append(
                ProviderAccount(
                    external_id=a["account_id"],
                    name=a.get("name") or a.get("official_name") or "Account",
                    type=map_account_type(a.get("type"), a.get("subtype")),
                    subtype=subtype,
                    mask=a.get("mask"),
                )
            )
        return out

    def fetch_balances(self, credentials: dict) -> list[ProviderBalance]:
        return [
            ProviderBalance(
                account_external_id=a["account_id"],
                current=_cents((a.get("balances") or {}).get("current")),
                available=_cents((a.get("balances") or {}).get("available")),
            )
            for a in self._accounts_raw(credentials)
        ]

    @staticmethod
    def _txn(t: dict) -> ProviderTransaction:
        pfc = t.get("personal_finance_category") or {}
        category = pfc.get("detailed") or pfc.get("primary")
        if not category and t.get("category"):
            category = " > ".join(t["category"])
        return ProviderTransaction(
            external_id=t["transaction_id"],
            account_external_id=t["account_id"],
            date=_date(t.get("date")),
            # Plaid: positive amount = money out. App: negative = money out.
            amount=-_cents(t["amount"]),
            payee=t.get("merchant_name") or t.get("name") or "",
            category=category,
            is_pending=bool(t.get("pending")),
            pending_external_id=t.get("pending_transaction_id"),
        )

    def fetch_transactions(self, credentials: dict, since: str | None) -> TransactionPage:
        """Cursor-based /transactions/sync. Pages until has_more is false; restarts the whole
        pagination run if Plaid reports the data changed mid-way (per Plaid's guidance)."""
        for _attempt in range(3):
            page = TransactionPage(next_cursor=since)
            cursor = since
            try:
                while True:
                    body = {"access_token": credentials["access_token"], "count": 500}
                    if cursor:
                        body["cursor"] = cursor
                    data = self._post("/transactions/sync", body)
                    page.added += [self._txn(t) for t in data.get("added", [])]
                    page.modified += [self._txn(t) for t in data.get("modified", [])]
                    page.removed += [r["transaction_id"] for r in data.get("removed", [])]
                    cursor = data.get("next_cursor") or cursor
                    if not data.get("has_more"):
                        break
            except ProviderError as exc:
                if exc.code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                    continue
                raise
            page.next_cursor = cursor
            return page
        raise ProviderError("Plaid transactions kept changing during sync; will retry next run.")

    def fetch_liabilities(self, credentials: dict) -> list[ProviderLiability]:
        try:
            data = self._post("/liabilities/get", {"access_token": credentials["access_token"]})
        except ProviderError as exc:
            if exc.code in UNSUPPORTED_CODES:
                return []
            raise
        lib = data.get("liabilities") or {}
        out = []
        for c in lib.get("credit") or []:
            aprs = c.get("aprs") or []
            purchase = next((a for a in aprs if a.get("apr_type") == "purchase_apr"), aprs[0] if aprs else None)
            out.append(
                ProviderLiability(
                    account_external_id=c["account_id"],
                    interest_rate=Decimal(str(purchase["apr_percentage"])) if purchase else None,
                    min_payment=_cents(c.get("minimum_payment_amount")),
                    next_payment_due=_date(c.get("next_payment_due_date")),
                )
            )
        for m in lib.get("mortgage") or []:
            rate = (m.get("interest_rate") or {}).get("percentage")
            out.append(
                ProviderLiability(
                    account_external_id=m["account_id"],
                    interest_rate=Decimal(str(rate)) if rate is not None else None,
                    min_payment=_cents(m.get("next_monthly_payment")),
                    next_payment_due=_date(m.get("next_payment_due_date")),
                )
            )
        for st in lib.get("student") or []:
            rate = st.get("interest_rate_percentage")
            out.append(
                ProviderLiability(
                    account_external_id=st["account_id"],
                    interest_rate=Decimal(str(rate)) if rate is not None else None,
                    min_payment=_cents(st.get("minimum_payment_amount")),
                    next_payment_due=_date(st.get("next_payment_due_date")),
                )
            )
        return out

    def fetch_holdings(self, credentials: dict) -> list[ProviderHolding]:
        try:
            data = self._post("/investments/holdings/get", {"access_token": credentials["access_token"]})
        except ProviderError as exc:
            if exc.code in UNSUPPORTED_CODES:
                return []
            raise
        securities = {s["security_id"]: s for s in data.get("securities") or []}
        out = []
        for h in data.get("holdings") or []:
            sec = securities.get(h.get("security_id"), {})
            out.append(
                ProviderHolding(
                    account_external_id=h["account_id"],
                    name=sec.get("name") or sec.get("ticker_symbol") or "Holding",
                    ticker=sec.get("ticker_symbol"),
                    quantity=Decimal(str(h["quantity"])) if h.get("quantity") is not None else None,
                    value=_cents(h.get("institution_value")),
                )
            )
        return out

    def check_status(self, credentials: dict) -> ConnectionHealth:
        data = self._post("/item/get", {"access_token": credentials["access_token"]})
        item = data.get("item") or {}
        err = item.get("error") or {}
        if err.get("error_code") in REAUTH_CODES:
            return ConnectionHealth("login_required", err.get("error_code"))
        if err.get("error_code"):
            return ConnectionHealth("error", err.get("error_code"))
        expiry = item.get("consent_expiration_time")
        if expiry:
            when = dt.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if when - dt.datetime.now(dt.timezone.utc) < dt.timedelta(days=7):
                return ConnectionHealth("pending_expiration", f"Access expires {when.date().isoformat()}")
        return ConnectionHealth("ok")

    def disconnect(self, credentials: dict) -> None:
        self._post("/item/remove", {"access_token": credentials["access_token"]})
