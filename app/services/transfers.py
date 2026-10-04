"""Transfer detection: pairs an outflow in one account with an equal inflow in another
account within a few days (e.g. chequing -> savings, chequing -> credit card payment).

Manual choices always win: a transaction with transfer_source == "manual" is never touched
by detection.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Transaction

MATCH_WINDOW_DAYS = 4


def detect_transfers(db: Session, since: dt.date | None = None) -> int:
    """Pair unflagged opposite-amount transactions across accounts. Returns pairs created."""
    q = select(Transaction).where(
        Transaction.is_transfer.is_(False),
        Transaction.transfer_source.is_(None),
        Transaction.is_pending.is_(False),
        Transaction.amount != 0,
    )
    if since is not None:
        q = q.where(Transaction.date >= since - dt.timedelta(days=MATCH_WINDOW_DAYS))
    candidates = db.scalars(q.order_by(Transaction.date, Transaction.id)).all()

    inflows: dict[int, list[Transaction]] = {}
    for t in candidates:
        if t.amount > 0:
            inflows.setdefault(t.amount, []).append(t)

    paired: set[int] = set()
    pairs = 0
    for out in candidates:
        if out.amount >= 0 or out.id in paired:
            continue
        best, best_gap = None, None
        for inc in inflows.get(-out.amount, []):
            if inc.id in paired or inc.account_id == out.account_id:
                continue
            gap = abs((inc.date - out.date).days)
            if gap <= MATCH_WINDOW_DAYS and (best_gap is None or gap < best_gap):
                best, best_gap = inc, gap
        if best is not None:
            for a, b in ((out, best), (best, out)):
                a.is_transfer, a.transfer_source, a.transfer_pair_id = True, "auto", b.id
            paired.update({out.id, best.id})
            pairs += 1
    return pairs


def set_transfer(db: Session, txn: Transaction, is_transfer: bool) -> None:
    """Manual override. Applies to the paired transaction too, so a pair stays consistent."""
    partner = db.get(Transaction, txn.transfer_pair_id) if txn.transfer_pair_id else None
    for t in filter(None, (txn, partner)):
        t.is_transfer = is_transfer
        t.transfer_source = "manual"
        if not is_transfer:
            t.transfer_pair_id = None


def unlink_partner(db: Session, txn: Transaction) -> None:
    """Call before deleting a transaction: an auto-detected partner goes back to normal."""
    if not txn.transfer_pair_id:
        return
    partner = db.get(Transaction, txn.transfer_pair_id)
    if partner is not None:
        partner.transfer_pair_id = None
        if partner.transfer_source == "auto":
            partner.is_transfer, partner.transfer_source = False, None
