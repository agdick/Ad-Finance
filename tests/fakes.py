"""A scriptable SyncProvider for tests (stands in for Plaid)."""
from __future__ import annotations

from app.providers.base import (
    LinkResult,
    ProviderAccount,
    ProviderBalance,
    ProviderError,
    SyncProvider,
    TransactionPage,
)


class FakeProvider(SyncProvider):
    name = "fake"
    display_name = "Fake bank"

    def __init__(self):
        self.accounts = [ProviderAccount("acc-chq", "Chequing", "chequing", mask="1234")]
        self.balances = {"acc-chq": 100000}
        self.pages: list[TransactionPage] = []
        self.fail: Exception | None = None
        self.seen_cursors: list[str | None] = []

    def is_configured(self):
        return True

    def link(self, existing_credentials=None):
        return {"kind": "fake", "token": "x"}

    def complete_link(self, payload):
        return LinkResult({"access_token": "access-sandbox-SECRET-TOKEN-123"}, "item-1", "Fake Bank")

    def _check(self):
        if self.fail:
            raise self.fail

    def fetch_accounts(self, credentials):
        self._check()
        assert credentials["access_token"] == "access-sandbox-SECRET-TOKEN-123"
        return self.accounts

    def fetch_transactions(self, credentials, since):
        self._check()
        self.seen_cursors.append(since)
        if not self.pages:
            return TransactionPage(next_cursor=since)
        page = self.pages.pop(0)
        page.next_cursor = page.next_cursor or f"cursor-{len(self.seen_cursors)}"
        return page

    def fetch_balances(self, credentials):
        self._check()
        return [ProviderBalance(k, v) for k, v in self.balances.items()]


class BrokenError(ProviderError):
    pass
