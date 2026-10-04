"""Database models. Amounts are integer cents (CAD); negative transaction amounts are outflows.

Transaction splitting is out of scope for v1. When it is added, introduce a
`transaction_splits` table (transaction_id, category_id, amount) and have
`services.budget.spending_lines()` read splits when present; nothing else in the
schema needs to change because transactions keep a stable integer id.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


ACCOUNT_TYPES = {
    "chequing": "Chequing",
    "credit": "Credit card",
    "savings": "Savings",
    "registered": "Registered (TFSA / RRSP / investment)",
    "loan": "Loan / debt",
}
LIABILITY_TYPES = {"credit", "loan"}


class ProviderConnection(Base):
    """One link to an aggregator (e.g. one Plaid Item). Credentials are encrypted at rest."""

    __tablename__ = "provider_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    institution_name: Mapped[str | None] = mapped_column(String(128))
    encrypted_credentials: Mapped[str] = mapped_column(Text)
    sync_cursor: Mapped[str | None] = mapped_column(Text)
    # ok | login_required | pending_expiration | error | disconnected
    status: Mapped[str] = mapped_column(String(32), default="ok")
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    accounts: Mapped[list[Account]] = relationship(back_populates="connection")

    @property
    def needs_reconnect(self) -> bool:
        return self.status in {"login_required", "pending_expiration"}


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    institution: Mapped[str | None] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))  # see ACCOUNT_TYPES
    subtype: Mapped[str | None] = mapped_column(String(64))  # e.g. TFSA, RRSP, mortgage
    mask: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16), default="manual")  # plaid | csv | manual
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("provider_connections.id", ondelete="SET NULL"))
    # As reported by the institution: for credit/loan accounts a positive balance is money owed.
    current_balance: Mapped[int | None] = mapped_column(Integer)
    available_balance: Mapped[int | None] = mapped_column(Integer)
    balance_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 3, asdecimal=True))
    min_payment: Mapped[int | None] = mapped_column(Integer)
    next_payment_due: Mapped[dt.date | None] = mapped_column(Date)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    connection: Mapped[ProviderConnection | None] = relationship(back_populates="accounts")
    holdings: Mapped[list[Holding]] = relationship(back_populates="account", cascade="all, delete-orphan")

    @property
    def is_liability(self) -> bool:
        return self.type in LIABILITY_TYPES

    @property
    def type_label(self) -> str:
        return ACCOUNT_TYPES.get(self.type, self.type)

    @property
    def display_name(self) -> str:
        return f"{self.name} ••{self.mask}" if self.mask else self.name


class BalanceSnapshot(Base):
    """Daily balance history, used for goal pace / payoff projections."""

    __tablename__ = "balance_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    balance: Mapped[int] = mapped_column(Integer)


class Holding(Base):
    __tablename__ = "holdings"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    ticker: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6, asdecimal=True))
    value: Mapped[int | None] = mapped_column(Integer)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped[Account] = relationship(back_populates="holdings")


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="expense")  # expense | income
    monthly_limit: Mapped[int | None] = mapped_column(Integer)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Comma-separated integer percentages, e.g. "80,100". NULL = use the global default.
    alert_thresholds: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    filename: Mapped[str | None] = mapped_column(String(256))
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    amount: Mapped[int] = mapped_column(Integer)  # cents; negative = outflow
    payee_raw: Mapped[str] = mapped_column(String(256), default="")
    payee_clean: Mapped[str] = mapped_column(String(256), default="", index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    category_status: Mapped[str] = mapped_column(String(16), default="suggested")  # suggested | confirmed
    suggestion_source: Mapped[str | None] = mapped_column(String(16))  # rule | history | provider | user
    provider_category: Mapped[str | None] = mapped_column(String(128))
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_source: Mapped[str | None] = mapped_column(String(16))  # auto | rule | manual
    transfer_pair_id: Mapped[int | None] = mapped_column(Integer)
    is_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16))  # plaid | csv | manual
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    import_fingerprint: Mapped[str | None] = mapped_column(String(512), index=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    account: Mapped[Account] = relationship()
    category: Mapped[Category | None] = relationship()


class PayeeRule(Base):
    __tablename__ = "payee_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_type: Mapped[str] = mapped_column(String(16), default="contains")  # exact | contains | starts_with
    match_value: Mapped[str] = mapped_column(String(256))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    mark_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    category: Mapped[Category | None] = relationship()


class RecurringItem(Base):
    __tablename__ = "recurring_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    payee_match: Mapped[str | None] = mapped_column(String(256))  # matched against payee_clean
    amount: Mapped[int] = mapped_column(Integer)  # expected, signed like transactions
    frequency: Mapped[str] = mapped_column(String(16))  # weekly | biweekly | monthly | quarterly | yearly
    next_due: Mapped[dt.date | None] = mapped_column(Date)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="detected")  # detected | confirmed | dismissed
    origin: Mapped[str] = mapped_column(String(16), default="manual")  # detected | manual
    last_seen: Mapped[dt.date | None] = mapped_column(Date)
    last_amount: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    category: Mapped[Category | None] = relationship()
    account: Mapped[Account | None] = relationship()


goal_accounts = Table(
    "goal_accounts",
    Base.metadata,
    Column("goal_id", ForeignKey("goals.id", ondelete="CASCADE"), primary_key=True),
    Column("account_id", ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
)


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))  # savings | debt
    target_amount: Mapped[int] = mapped_column(Integer)
    target_date: Mapped[dt.date | None] = mapped_column(Date)
    # Savings goals: "accounts" (sum of linked balances) or "contributions" (manual entries).
    progress_source: Mapped[str] = mapped_column(String(16), default="accounts")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    accounts: Mapped[list[Account]] = relationship(secondary=goal_accounts)
    contributions: Mapped[list[GoalContribution]] = relationship(
        back_populates="goal", cascade="all, delete-orphan", order_by="GoalContribution.date"
    )


class GoalContribution(Base):
    __tablename__ = "goal_contributions"
    id: Mapped[int] = mapped_column(primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(256))

    goal: Mapped[Goal] = relationship(back_populates="contributions")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("category_id", "month", "threshold"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    threshold: Mapped[int] = mapped_column(Integer)
    spent: Mapped[int] = mapped_column(Integer)  # spending at the moment it fired
    limit: Mapped[int] = mapped_column(Integer)
    fired_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    category: Mapped[Category] = relationship()


class ImportMapping(Base):
    __tablename__ = "import_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution: Mapped[str] = mapped_column(String(128), unique=True)
    column_mapping: Mapped[str] = mapped_column(Text)  # JSON, see services.csv_import.Mapping


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
