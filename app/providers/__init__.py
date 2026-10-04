"""Provider registry. Register new SyncProvider implementations here."""
from __future__ import annotations

from .base import SyncProvider
from .plaid import PlaidProvider

_REGISTRY: dict[str, SyncProvider] = {}


def register(provider: SyncProvider) -> None:
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> SyncProvider:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"No sync provider named {name!r}") from None


def all_providers() -> list[SyncProvider]:
    return list(_REGISTRY.values())


register(PlaidProvider())
