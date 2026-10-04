from __future__ import annotations

import os
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Account, ImportBatch, ImportMapping
from ..security import verify_csrf
from ..services import pipeline
from ..services.csv_import import (
    DATE_FORMATS,
    Mapping,
    decode,
    guess_mapping,
    import_rows,
    load_mapping,
    parse_rows,
    read_table,
    save_mapping,
)
from .common import active_accounts, flash, redirect, render

router = APIRouter()
MAX_UPLOAD = 10 * 1024 * 1024
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _upload_dir() -> str:
    path = os.path.join(get_settings().data_dir, "imports")
    os.makedirs(path, exist_ok=True)
    return path


def _upload_path(token: str) -> str:
    if not _TOKEN.match(token):
        raise HTTPException(400, "Bad upload token")
    return os.path.join(_upload_dir(), f"{token}.csv")


@router.get("/import")
def import_start(request: Request, account: int | None = None, db: Session = Depends(get_db)):
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(10)).all()
    mappings = db.scalars(select(ImportMapping).order_by(ImportMapping.institution)).all()
    return render(
        request,
        db,
        "import/start.html",
        {"accounts": active_accounts(db), "selected": account, "batches": batches, "mappings": mappings},
    )


@router.post("/import/upload", dependencies=[Depends(verify_csrf)])
async def import_upload(
    request: Request,
    account_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if account is None:
        flash(request, "Choose an account first.", "error")
        return redirect("/import")
    data = await file.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD:
        flash(request, "The file is empty or larger than 10 MB.", "error")
        return redirect("/import", account=account_id)
    token = uuid.uuid4().hex
    with open(_upload_path(token), "w", encoding="utf-8") as fh:
        fh.write(decode(data))
    request.session["import_name"] = (file.filename or "upload.csv")[:200]
    mapping = load_mapping(db, account.institution) or guess_mapping(read_table(decode(data))[0])
    return _mapping_page(request, db, account, token, mapping, account.institution or "", saved=bool(load_mapping(db, account.institution)))


def _mapping_page(request, db, account, token, mapping: Mapping, institution: str, saved: bool = False, status: int = 200):
    with open(_upload_path(token), encoding="utf-8") as fh:
        text = fh.read()
    header, raw_rows = read_table(text, mapping)
    problems = mapping.problems()
    parsed = parse_rows(text, mapping) if not problems else None
    return render(
        request,
        db,
        "import/map.html",
        {
            "account": account,
            "token": token,
            "mapping": mapping,
            "institution": institution,
            "header": header,
            "raw_rows": raw_rows[:8],
            "row_count": len(raw_rows),
            "problems": problems,
            "parsed": parsed,
            "date_formats": DATE_FORMATS,
            "saved": saved,
            "filename": request.session.get("import_name"),
        },
        status,
    )


def _mapping_from_form(form) -> Mapping:
    skip = str(form.get("skip_rows", "0"))
    return Mapping(
        date_col=str(form.get("date_col", "")),
        description_cols=[str(v) for v in form.getlist("description_cols") if v],
        amount_col=str(form.get("amount_col", "")) if form.get("amount_mode", "single") == "single" else "",
        debit_col=str(form.get("debit_col", "")) if form.get("amount_mode") == "split" else "",
        credit_col=str(form.get("credit_col", "")) if form.get("amount_mode") == "split" else "",
        balance_col=str(form.get("balance_col", "")),
        category_col=str(form.get("category_col", "")),
        date_format=str(form.get("date_format", "auto")) or "auto",
        invert_amounts=bool(form.get("invert_amounts")),
        has_header=form.get("has_header") is not None,
        skip_rows=int(skip) if skip.isdigit() else 0,
    )


@router.post("/import/map", dependencies=[Depends(verify_csrf)])
async def import_map(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    token = str(form.get("token", ""))
    path = _upload_path(token)
    account = db.get(Account, int(form.get("account_id", 0) or 0))
    if account is None or not os.path.exists(path):
        flash(request, "That upload has expired; please choose the file again.", "error")
        return redirect("/import")
    mapping = _mapping_from_form(form)
    institution = str(form.get("institution", "")).strip()
    if form.get("action") != "import":
        return _mapping_page(request, db, account, token, mapping, institution)

    problems = mapping.problems()
    with open(path, encoding="utf-8") as fh:
        parsed = parse_rows(fh.read(), mapping) if not problems else None
    if problems or parsed is None or not parsed.rows:
        return _mapping_page(request, db, account, token, mapping, institution, status=400)
    if mapping.date_format == "auto":
        mapping.date_format = parsed.date_format or "auto"
    if form.get("save_mapping") and institution:
        save_mapping(db, institution, mapping)
        if not account.institution:
            account.institution = institution
    result = import_rows(db, account, parsed.rows, request.session.get("import_name"))
    pipeline.after_ingest(db, result.new_transactions)
    db.commit()
    os.remove(path)
    skipped = f" {len(parsed.errors)} row(s) could not be read." if parsed.errors else ""
    flash(
        request,
        f"Imported {result.inserted} new transaction{'s' if result.inserted != 1 else ''} into {account.name}; "
        f"{result.duplicates} duplicate{'s' if result.duplicates != 1 else ''} skipped.{skipped}",
    )
    dates = [r.date for r in parsed.rows]
    return redirect("/transactions", account=account.id, start=min(dates).isoformat(), end=max(dates).isoformat())


@router.post("/import/mappings/{mapping_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_mapping(mapping_id: int, db: Session = Depends(get_db)):
    m = db.get(ImportMapping, mapping_id)
    if m is not None:
        db.delete(m)
        db.commit()
    return redirect("/import")
