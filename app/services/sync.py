"""Turns SyncProvider output into rows in the local database.

Only this module talks to providers. Failures are recorded on the connection (and shown
as a reconnect prompt where relevant); they never affect CSV import or manual entry.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, Holding, ProviderConnection, Transaction, utcnow
from ..providers import get_provider
from ..providers.base import ProviderAuthError, ProviderError, ProviderTransaction
from ..security import CredentialError, decrypt_credentials, encrypt_credentials
from . import transfers
from .balances import record_balance
from .dates import today
from .dedupe import match_for_synced
from .payees import clean_payee
from .pipeline import after_ingest

log = logging.getLogger(__name__)
_sync_lock = threading.Lock()


@dataclass
class SyncReport:
    connection_id: int
    ok: bool
    added: int = 0
    updated: int = 0
    removed: int = 0
    message: str | None = None
    new_transactions: list[Transaction] = field(default_factory=list)


def start_link(db: Session, provider_name: str, connection_id: int | None = None) -> dict:
    provider = get_provider(provider_name)
    existing = None
    if connection_id is not None:
        conn = db.get(ProviderConnection, connection_id)
        if conn is None or conn.provider != provider_name:
            raise ProviderError("Unknown connection.")
        existing = decrypt_credentials(conn.encrypted_credentials)
    return provider.link(existing)


def complete_link(db: Session, provider_name: str, payload: dict) -> ProviderConnection:
    provider = get_provider(provider_name)
    result = provider.complete_link(payload)
    conn = db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.provider == provider_name, ProviderConnection.external_id == result.external_id
        )
    )
    if conn is None:
        conn = ProviderConnection(provider=provider_name, external_id=result.external_id)
        db.add(conn)
    conn.encrypted_credentials = encrypt_credentials(result.credentials)
    conn.institution_name = result.institution_name or conn.institution_name
    conn.status, conn.status_detail = "ok", None
    db.commit()
    return conn


def mark_relinked(db: Session, conn: ProviderConnection) -> None:
    """After a successful update-mode re-link the same credentials work again."""
    conn.status, conn.status_detail = "ok", None
    db.commit()


def _upsert_accounts(db: Session, conn: ProviderConnection, provider_accounts) -> dict[str, Account]:
    by_ext = {a.external_id: a for a in db.scalars(select(Account).where(Account.connection_id == conn.id))}
    for pa in provider_accounts:
        acct = by_ext.get(pa.external_id)
        if acct is None:
            acct = Account(
                name=pa.name,
                type=pa.type,
                source=conn.provider,
                external_id=pa.external_id,
                connection_id=conn.id,
            )
            db.add(acct)
            by_ext[pa.external_id] = acct
        acct.institution = pa.institution or conn.institution_name or acct.institution
        acct.subtype = pa.subtype
        acct.mask = pa.mask
    db.flush()
    return by_ext


def _apply_fields(txn: Transaction, pt: ProviderTransaction) -> None:
    txn.date = pt.date
    txn.amount = pt.amount
    txn.payee_raw = pt.payee[:256]
    if txn.category_status != "confirmed":
        txn.payee_clean = clean_payee(pt.payee)
    elif not txn.payee_clean:
        txn.payee_clean = clean_payee(pt.payee)
    txn.provider_category = pt.category
    txn.is_pending = pt.is_pending


def _by_external_id(db: Session, external_id: str | None) -> Transaction | None:
    if not external_id:
        return None
    return db.scalar(select(Transaction).where(Transaction.external_id == external_id))


def apply_transactions(db: Session, accounts: dict[str, Account], page, report: SyncReport, source: str) -> None:
    # Added, then modified, then removed: a posted transaction replaces its pending
    # version in place (keeping the user's category/note), so nothing is double-counted.
    for pt in page.added:
        acct = accounts.get(pt.account_external_id)
        if acct is None:
            continue
        txn = _by_external_id(db, pt.external_id) or _by_external_id(db, pt.pending_external_id)
        if txn is None and not pt.is_pending:
            txn = match_for_synced(db, acct.id, pt.date, pt.amount)
        if txn is None:
            txn = Transaction(account_id=acct.id, source=source, category_status="suggested")
            db.add(txn)
            _apply_fields(txn, pt)
            txn.external_id = pt.external_id
            report.added += 1
            report.new_transactions.append(txn)
            continue
        was_pending = txn.is_pending
        _apply_fields(txn, pt)
        txn.external_id = pt.external_id
        report.updated += 1
        if was_pending and not pt.is_pending:
            # Now posted: it may also have been imported by CSV in the meantime.
            db.flush()
            dup = match_for_synced(db, acct.id, txn.date, txn.amount)
            if dup is not None and dup.id != txn.id:
                if txn.category_status != "confirmed" and dup.category_status == "confirmed":
                    txn.category_id, txn.category_status = dup.category_id, "confirmed"
                txn.import_fingerprint = txn.import_fingerprint or dup.import_fingerprint
                txn.note = txn.note or dup.note
                transfers.unlink_partner(db, dup)
                db.delete(dup)
    for pt in page.modified:
        txn = _by_external_id(db, pt.external_id)
        if txn is not None:
            _apply_fields(txn, pt)
            report.updated += 1
    db.flush()
    for ext_id in page.removed:
        txn = _by_external_id(db, ext_id)
        if txn is not None:
            transfers.unlink_partner(db, txn)
            db.delete(txn)
            report.removed += 1
    db.flush()


def sync_connection(db: Session, conn: ProviderConnection) -> SyncReport:
    report = SyncReport(connection_id=conn.id, ok=False)
    provider = get_provider(conn.provider)
    if conn.status == "disconnected":
        report.message = "Connection was removed."
        return report
    try:
        creds = decrypt_credentials(conn.encrypted_credentials)
        accounts = _upsert_accounts(db, conn, provider.fetch_accounts(creds))
        page = provider.fetch_transactions(creds, conn.sync_cursor)
        apply_transactions(db, accounts, page, report, conn.provider)
        now, on = utcnow(), today()
        for bal in provider.fetch_balances(creds):
            acct = accounts.get(bal.account_external_id)
            if acct is not None:
                record_balance(db, acct, bal.current, on)
                acct.available_balance = bal.available
                acct.last_synced_at = now
        _sync_extras(db, provider, creds, accounts)
        conn.sync_cursor = page.next_cursor
        conn.last_synced_at = now
        try:
            health = provider.check_status(creds)
            conn.status, conn.status_detail = health.status, health.detail
        except ProviderAuthError:
            raise
        except ProviderError as exc:
            log.info("Status check failed (sync data kept): %s", exc.code)
            conn.status, conn.status_detail = "ok", None
        db.flush()
        after_ingest(db, report.new_transactions)
        db.commit()
        report.ok = True
    except ProviderAuthError as exc:
        db.rollback()
        _record_failure(db, conn, "login_required", str(exc))
        report.message = str(exc)
    except (ProviderError, CredentialError) as exc:
        db.rollback()
        _record_failure(db, conn, "error", str(exc))
        report.message = str(exc)
    except Exception as exc:  # keep the scheduler alive; never leak details
        db.rollback()
        log.exception("Unexpected error syncing connection %s", conn.id)
        _record_failure(db, conn, "error", f"Unexpected error ({type(exc).__name__}); see server logs.")
        report.message = "Unexpected error"
    return report


def _sync_extras(db: Session, provider, creds: dict, accounts: dict[str, Account]) -> None:
    """Liabilities and holdings are nice-to-have; failures here don't fail the sync."""
    try:
        for lib in provider.fetch_liabilities(creds):
            acct = accounts.get(lib.account_external_id)
            if acct is not None:
                acct.interest_rate = lib.interest_rate
                acct.min_payment = lib.min_payment
                acct.next_payment_due = lib.next_payment_due
    except ProviderAuthError:
        raise
    except ProviderError as exc:
        log.info("Liabilities unavailable: %s", exc.code)
    try:
        holdings = provider.fetch_holdings(creds)
    except ProviderAuthError:
        raise
    except ProviderError as exc:
        log.info("Holdings unavailable: %s", exc.code)
        return
    touched = {h.account_external_id for h in holdings}
    for ext_id in touched:
        acct = accounts.get(ext_id)
        if acct is None:
            continue
        acct.holdings.clear()
        for h in holdings:
            if h.account_external_id == ext_id:
                acct.holdings.append(Holding(name=h.name[:256], ticker=h.ticker, quantity=h.quantity, value=h.value))


def _record_failure(db: Session, conn: ProviderConnection, status: str, detail: str) -> None:
    conn = db.get(ProviderConnection, conn.id)
    conn.status, conn.status_detail = status, detail[:500]
    db.commit()


def sync_all(db: Session) -> list[SyncReport]:
    if not _sync_lock.acquire(blocking=False):
        return []
    try:
        conns = db.scalars(select(ProviderConnection).where(ProviderConnection.status != "disconnected")).all()
        reports = []
        for conn in conns:
            try:
                if not get_provider(conn.provider).is_configured():
                    continue
            except KeyError:
                continue
            reports.append(sync_connection(db, conn))
        return reports
    finally:
        _sync_lock.release()


def disconnect(db: Session, conn: ProviderConnection) -> None:
    """Remove the link at the provider (best effort). Accounts and history stay local."""
    try:
        get_provider(conn.provider).disconnect(decrypt_credentials(conn.encrypted_credentials))
    except (ProviderError, CredentialError, KeyError) as exc:
        log.info("Provider disconnect failed (continuing): %s", exc)
    conn.status, conn.status_detail = "disconnected", None
    conn.encrypted_credentials = ""
    conn.sync_cursor = None
    db.commit()
