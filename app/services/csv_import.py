"""CSV / statement import with saved per-institution column mappings."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections import OrderedDict
from dataclasses import asdict, dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, ImportBatch, ImportMapping, Transaction
from ..money import MoneyError, parse_cents
from .balances import record_balance
from .dedupe import fingerprint, match_for_csv_row
from .payees import clean_payee

DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m/%d/%y",
    "%d/%m/%y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
]


@dataclass
class Mapping:
    date_col: str = ""
    description_cols: list[str] = field(default_factory=list)
    amount_col: str = ""  # single signed amount column...
    debit_col: str = ""  # ...or separate money-out / money-in columns
    credit_col: str = ""
    balance_col: str = ""
    category_col: str = ""
    date_format: str = "auto"
    # True when the file shows purchases as positive numbers (common on credit card exports).
    invert_amounts: bool = False
    has_header: bool = True
    skip_rows: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Mapping:
        data = json.loads(text)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def problems(self) -> list[str]:
        out = []
        if not self.date_col:
            out.append("Choose the date column.")
        if not self.description_cols:
            out.append("Choose at least one description column.")
        if not self.amount_col and not (self.debit_col or self.credit_col):
            out.append("Choose an amount column, or debit/credit columns.")
        return out


@dataclass
class ParsedRow:
    line: int
    date: dt.date
    amount: int
    description: str
    balance: int | None = None
    category: str | None = None


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    errors: list[str]
    date_format: str | None


@dataclass
class ImportResult:
    batch_id: int
    total: int
    inserted: int
    duplicates: int
    errors: list[str]
    new_transactions: list[Transaction]


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def read_table(text: str, mapping: Mapping | None = None) -> tuple[list[str], list[list[str]]]:
    """Return (column names, data rows). Without a header, columns are 'Column 1', 'Column 2'..."""
    skip = mapping.skip_rows if mapping else 0
    has_header = mapping.has_header if mapping else True
    lines = text.splitlines()[skip:]
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO("\n".join(lines)), dialect) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    width = max(len(r) for r in rows)
    if has_header:
        header = [h.strip() or f"Column {i + 1}" for i, h in enumerate(rows[0])]
        header += [f"Column {i + 1}" for i in range(len(header), width)]
        data = rows[1:]
    else:
        header = [f"Column {i + 1}" for i in range(width)]
        data = rows
    # De-duplicate repeated header names so every column is addressable.
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        if h in seen:
            seen[h] += 1
            header[i] = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
    return header, [r + [""] * (width - len(r)) for r in data]


def detect_date_format(values: list[str]) -> str | None:
    """The format that parses the most values (earlier formats win ties). Stray footer or
    junk rows don't block detection; they're reported as row errors instead."""
    values = [v.strip() for v in values if v.strip()]
    best, best_hits = None, 0
    for fmt in DATE_FORMATS:
        hits = 0
        for v in values:
            try:
                dt.datetime.strptime(v, fmt)
                hits += 1
            except ValueError:
                pass
        if hits > best_hits:
            best, best_hits = fmt, hits
    return best if best_hits * 2 >= len(values) else None


def guess_mapping(header: list[str]) -> Mapping:
    """Best-effort initial mapping from header names (user reviews it before importing)."""
    low = {h: h.lower() for h in header}

    def find(*words: str, exclude: tuple[str, ...] = ()) -> str:
        for h, l in low.items():
            if any(w in l for w in words) and not any(x in l for x in exclude):
                return h
        return ""

    m = Mapping()
    m.date_col = find("transaction date", "posted date", "date")
    desc = [h for h, l in low.items() if "description" in l or "payee" in l or "merchant" in l or l in {"name", "transaction", "details", "memo"}]
    m.description_cols = desc[:2]
    m.amount_col = find("cad$", "amount", exclude=("usd",))
    if not m.amount_col:
        m.debit_col = find("debit", "withdrawal", "money out", "paid out")
        m.credit_col = find("credit", "deposit", "money in", "paid in")
    m.balance_col = find("balance")
    m.category_col = find("category")
    return m


def _cell(row: list[str], header: list[str], col: str) -> str:
    if not col or col not in header:
        return ""
    return row[header.index(col)].strip()


def parse_rows(text: str, mapping: Mapping) -> ParseResult:
    header, data = read_table(text, mapping)
    errors: list[str] = []
    fmt = mapping.date_format
    if fmt == "auto":
        fmt = detect_date_format([_cell(r, header, mapping.date_col) for r in data])
        if fmt is None:
            return ParseResult([], ["Could not recognise the date format; pick one explicitly."], None)
    rows = []
    first_line = mapping.skip_rows + (2 if mapping.has_header else 1)
    for i, r in enumerate(data):
        line = first_line + i
        raw_date = _cell(r, header, mapping.date_col)
        if not raw_date:
            continue  # blank / footer row
        try:
            date = dt.datetime.strptime(raw_date, fmt).date()
        except ValueError:
            errors.append(f"Line {line}: unrecognised date {raw_date!r}")
            continue
        try:
            if mapping.amount_col:
                amount = parse_cents(_cell(r, header, mapping.amount_col))
            else:
                debit = _cell(r, header, mapping.debit_col)
                credit = _cell(r, header, mapping.credit_col)
                amount = (parse_cents(credit) if credit else 0) - (abs(parse_cents(debit)) if debit else 0)
                if not debit and not credit:
                    raise MoneyError("no amount")
        except MoneyError:
            errors.append(f"Line {line}: could not read the amount")
            continue
        if mapping.invert_amounts:
            amount = -amount
        description = " ".join(p for p in (_cell(r, header, c) for c in mapping.description_cols) if p)
        balance = None
        if mapping.balance_col and _cell(r, header, mapping.balance_col):
            try:
                balance = parse_cents(_cell(r, header, mapping.balance_col))
            except MoneyError:
                balance = None
        category = _cell(r, header, mapping.category_col) or None
        rows.append(ParsedRow(line, date, amount, description, balance, category))
    return ParseResult(rows, errors, fmt)


def import_rows(db: Session, account: Account, rows: list[ParsedRow], filename: str | None) -> ImportResult:
    batch = ImportBatch(account_id=account.id, filename=filename, rows_total=len(rows))
    db.add(batch)
    db.flush()

    groups: OrderedDict[str, list[ParsedRow]] = OrderedDict()
    for row in rows:
        groups.setdefault(fingerprint(row.date, row.amount, row.description), []).append(row)

    new: list[Transaction] = []
    duplicates = 0
    claimed: set[int] = set()
    for fp, group in groups.items():
        already = db.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.account_id == account.id, Transaction.import_fingerprint == fp
            )
        )
        duplicates += min(already, len(group))
        for row in group[already:]:
            match = match_for_csv_row(db, account.id, row.date, row.amount, claimed)
            if match is not None:
                match.import_fingerprint = fp  # same transaction already synced/entered
                claimed.add(match.id)
                duplicates += 1
                continue
            txn = Transaction(
                account_id=account.id,
                date=row.date,
                amount=row.amount,
                payee_raw=row.description[:256],
                payee_clean=clean_payee(row.description),
                provider_category=row.category,
                source="csv",
                import_fingerprint=fp,
                import_batch_id=batch.id,
                category_status="suggested",
            )
            db.add(txn)
            new.append(txn)
    db.flush()

    with_balance = [r for r in rows if r.balance is not None]
    if with_balance and account.source != "plaid":
        latest = max(with_balance, key=lambda r: (r.date, r.line))
        record_balance(db, account, latest.balance, latest.date)

    batch.inserted, batch.duplicates = len(new), duplicates
    return ImportResult(batch.id, len(rows), len(new), duplicates, [], new)


def save_mapping(db: Session, institution: str, mapping: Mapping) -> None:
    institution = institution.strip()
    if not institution:
        return
    row = db.scalar(select(ImportMapping).where(func.lower(ImportMapping.institution) == institution.lower()))
    if row:
        row.column_mapping = mapping.to_json()
    else:
        db.add(ImportMapping(institution=institution, column_mapping=mapping.to_json()))


def load_mapping(db: Session, institution: str | None) -> Mapping | None:
    if not institution:
        return None
    row = db.scalar(select(ImportMapping).where(func.lower(ImportMapping.institution) == institution.strip().lower()))
    return Mapping.from_json(row.column_mapping) if row else None
