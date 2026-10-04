"""Full data export (CSV of transactions; JSON of every table). Credentials are never exported."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Base
from ..models import Transaction
from ..money import cents_to_input

EXCLUDED_COLUMNS = {("provider_connections", "encrypted_credentials"), ("provider_connections", "sync_cursor")}

CSV_COLUMNS = [
    "id",
    "date",
    "account",
    "institution",
    "amount",
    "payee",
    "payee_raw",
    "category",
    "category_status",
    "is_transfer",
    "is_pending",
    "source",
    "note",
]


def transactions_csv(db: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for t in db.scalars(select(Transaction).order_by(Transaction.date, Transaction.id)):
        w.writerow(
            [
                t.id,
                t.date.isoformat(),
                t.account.name,
                t.account.institution or "",
                cents_to_input(t.amount),
                t.payee_clean,
                t.payee_raw,
                t.category.name if t.category else "",
                t.category_status,
                "yes" if t.is_transfer else "no",
                "yes" if t.is_pending else "no",
                t.source,
                t.note or "",
            ]
        )
    return buf.getvalue()


def _jsonable(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def full_json(db: Session) -> str:
    out: dict = {
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "currency": "CAD",
        "amount_unit": "cents",
        "tables": {},
    }
    for name, table in sorted(Base.metadata.tables.items()):
        cols = [c for c in table.columns if (name, c.name) not in EXCLUDED_COLUMNS]
        rows = db.execute(select(*cols)).mappings().all()
        out["tables"][name] = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    return json.dumps(out, indent=2)
