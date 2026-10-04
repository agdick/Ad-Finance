"""Category suggestions: (1) payee rules, (2) learned history, (3) the data source's category.

New transactions are always left as "suggested"; the user confirms them (one at a time or
in bulk) in the review queue.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Category, PayeeRule, Transaction
from .payees import payee_key

# Default mapping from Plaid personal-finance-category codes (longest prefix wins) to the
# starter category names. Only used when no rule or history exists.
PROVIDER_CATEGORY_DEFAULTS: dict[str, str] = {
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "FOOD_AND_DRINK": "Dining Out",
    "TRANSPORTATION": "Transportation",
    "TRAVEL": "Travel",
    "RENT_AND_UTILITIES_RENT": "Housing",
    "RENT_AND_UTILITIES_TELEPHONE": "Phone & Internet",
    "RENT_AND_UTILITIES_INTERNET_AND_CABLE": "Phone & Internet",
    "RENT_AND_UTILITIES": "Utilities",
    "HOME_IMPROVEMENT": "Housing",
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT": "Housing",
    "MEDICAL": "Health",
    "PERSONAL_CARE": "Personal Care",
    "ENTERTAINMENT": "Entertainment",
    "GENERAL_MERCHANDISE": "Shopping",
    "GENERAL_SERVICES_INSURANCE": "Insurance",
    "BANK_FEES": "Fees & Interest",
    "GOVERNMENT_AND_NON_PROFIT_DONATIONS": "Gifts & Donations",
    "INCOME_WAGES": "Paycheque",
    "INCOME": "Other Income",
}


def rule_matches(rule: PayeeRule, payee_clean: str, payee_raw: str = "") -> bool:
    value = payee_key(rule.match_value)
    if not value:
        return False
    clean, raw = payee_key(payee_clean), payee_key(payee_raw)
    if rule.match_type == "exact":
        return clean == value
    if rule.match_type == "starts_with":
        return clean.startswith(value) or raw.startswith(value)
    return value in clean or value in raw


def _rule_order(rule: PayeeRule):
    rank = {"exact": 0, "starts_with": 1, "contains": 2}.get(rule.match_type, 3)
    return (rank, -len(rule.match_value), -rule.id)


def load_rules(db: Session) -> list[PayeeRule]:
    return sorted(db.scalars(select(PayeeRule)).all(), key=_rule_order)


def find_rule(rules: list[PayeeRule], txn: Transaction) -> PayeeRule | None:
    for rule in rules:
        if rule_matches(rule, txn.payee_clean, txn.payee_raw):
            return rule
    return None


def history_category(db: Session, payee_clean: str, exclude_id: int | None = None) -> int | None:
    """Most-used confirmed category for this payee."""
    key = payee_key(payee_clean)
    if not key:
        return None
    q = (
        select(Transaction.category_id, func.count().label("n"))
        .join(Category, Category.id == Transaction.category_id)
        .where(
            func.lower(Transaction.payee_clean) == key,
            Transaction.category_status == "confirmed",
            Transaction.is_transfer.is_(False),
            Category.archived.is_(False),
        )
        .group_by(Transaction.category_id)
        .order_by(func.count().desc(), func.max(Transaction.date).desc())
    )
    if exclude_id is not None:
        q = q.where(Transaction.id != exclude_id)
    row = db.execute(q.limit(1)).first()
    return row[0] if row else None


def provider_category_guess(db: Session, provider_category: str | None) -> int | None:
    if not provider_category:
        return None
    # Learned: what the user usually confirms for this source category.
    row = db.execute(
        select(Transaction.category_id, func.count())
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.provider_category == provider_category,
            Transaction.category_status == "confirmed",
            Category.archived.is_(False),
        )
        .group_by(Transaction.category_id)
        .order_by(func.count().desc())
        .limit(1)
    ).first()
    if row:
        return row[0]
    names = {c.name.lower(): c.id for c in db.scalars(select(Category).where(Category.archived.is_(False)))}
    # A source category that is literally one of ours (e.g. a CSV "Category" column).
    if provider_category.strip().lower() in names:
        return names[provider_category.strip().lower()]
    code = provider_category.upper().replace(" ", "_")
    for prefix in sorted(PROVIDER_CATEGORY_DEFAULTS, key=len, reverse=True):
        if code.startswith(prefix):
            return names.get(PROVIDER_CATEGORY_DEFAULTS[prefix].lower())
    return None


def suggest(db: Session, txn: Transaction, rules: list[PayeeRule] | None = None) -> None:
    """Fill in a suggested category (and rule-based transfer flag) for an unconfirmed transaction."""
    if txn.category_status == "confirmed":
        return
    rules = load_rules(db) if rules is None else rules
    rule = find_rule(rules, txn)
    if rule is not None:
        if rule.mark_transfer and txn.transfer_source != "manual":
            txn.is_transfer = True
            txn.transfer_source = "rule"
        if rule.category_id is not None:
            txn.category_id, txn.suggestion_source = rule.category_id, "rule"
            return
    cat = history_category(db, txn.payee_clean, exclude_id=txn.id)
    if cat is not None:
        txn.category_id, txn.suggestion_source = cat, "history"
        return
    cat = provider_category_guess(db, txn.provider_category)
    if cat is not None:
        txn.category_id, txn.suggestion_source = cat, "provider"
        return
    txn.category_id, txn.suggestion_source = None, None


def suggest_many(db: Session, txns: Iterable[Transaction]) -> None:
    rules = load_rules(db)
    for txn in txns:
        suggest(db, txn, rules)


def resuggest_unconfirmed(db: Session) -> int:
    txns = db.scalars(select(Transaction).where(Transaction.category_status == "suggested")).all()
    suggest_many(db, txns)
    return len(txns)


def confirm(txn: Transaction, category_id: int | None) -> None:
    if category_id != txn.category_id:
        txn.suggestion_source = "user"
    txn.category_id = category_id
    txn.category_status = "confirmed"


def create_rule_for_payee(db: Session, payee_clean: str, category_id: int) -> PayeeRule:
    """'Always categorize this payee as X'. Replaces an existing exact rule for the payee and
    re-suggests every unconfirmed transaction so the rule takes effect immediately."""
    key = payee_key(payee_clean)
    rule = next(
        (r for r in db.scalars(select(PayeeRule).where(PayeeRule.match_type == "exact")) if payee_key(r.match_value) == key),
        None,
    )
    if rule is None:
        rule = PayeeRule(match_type="exact", match_value=payee_clean)
        db.add(rule)
    rule.category_id = category_id
    db.flush()
    resuggest_unconfirmed(db)
    return rule
