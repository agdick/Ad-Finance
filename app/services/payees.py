"""Turn raw bank descriptions into stable payee names for matching rules and history."""
from __future__ import annotations

import re

_PREFIXES = re.compile(
    r"^(?:(?:pos|visa|debit|interac|idp|purchase|pre-?auth(?:orized)?|pap|e-?transfer|"
    r"point of sale|contactless|tap|apple pay|google pay|sq \*|sq\*|tst\*|tst \*|pp\*|paypal \*)\s*[-:#*]?\s*)+",
    re.IGNORECASE,
)
_NOISE = [
    re.compile(r"#\s*\d+"),  # store numbers
    re.compile(r"\b\d{4,}\b"),  # long digit runs (card / ref numbers)
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # embedded dates
    re.compile(r"\*+\w*"),  # masked fragments
    re.compile(r"\b(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b\s*(?:CA|CAN)?\s*$"),  # trailing province
]
_SPACES = re.compile(r"\s+")
_SMALL = {"and", "of", "the", "de", "du", "la", "le", "for"}


def clean_payee(raw: str | None) -> str:
    if not raw:
        return ""
    s = _PREFIXES.sub("", raw.strip())
    for pattern in _NOISE:
        s = pattern.sub(" ", s)
    s = _SPACES.sub(" ", s).strip(" -*#.,")
    if not s:
        s = raw.strip()
    # Title-case shouty bank text, keep mixed-case merchant names (e.g. "McDonald's") as is.
    if s.isupper() or s.islower():
        words = s.lower().split(" ")
        s = " ".join(w if (i and w in _SMALL) else w[:1].upper() + w[1:] for i, w in enumerate(words))
    return s[:256]


def payee_key(name: str | None) -> str:
    """Case/space-insensitive key used for history and rule matching."""
    return _SPACES.sub(" ", (name or "").strip().lower())
