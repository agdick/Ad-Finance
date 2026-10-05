"""One test (or group) per acceptance criterion in the handoff sheet, section 13."""
from __future__ import annotations

import datetime as dt
import json
import logging

import pytest
from sqlalchemy import select

from app.models import Alert, ProviderConnection, Transaction
from app.providers import register
from app.providers.base import ProviderAccount, ProviderAuthError, ProviderTransaction, TransactionPage
from app.security import encrypt_credentials
from app.services import alerts, categorize, sync
from app.services.budget import month_summary
from app.services.csv_import import Mapping, import_rows, parse_rows
from app.services.pipeline import after_ingest
from tests.fakes import FakeProvider
from tests.helpers import account, cat, count, txn

RBC_CSV = """"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2","CAD$","USD$"
Chequing,01234-5678901,10/2/2026,,"LOBLAWS #1234","TORONTO ON",-84.12,
Chequing,01234-5678901,10/3/2026,,"TIM HORTONS #0099",,-2.45,
Chequing,01234-5678901,10/3/2026,,"TIM HORTONS #0099",,-2.45,
Chequing,01234-5678901,10/9/2026,,"PAYROLL DEP","ACME CORP",2450.00,
"""
RBC_MAPPING = Mapping(date_col="Transaction Date", description_cols=["Description 1", "Description 2"], amount_col="CAD$")


def _import(db, acct, text=RBC_CSV, mapping=RBC_MAPPING):
    parsed = parse_rows(text, mapping)
    assert not parsed.errors
    result = import_rows(db, acct, parsed.rows, "rbc.csv")
    after_ingest(db, result.new_transactions)
    db.commit()
    return result


@pytest.fixture()
def fake(db):
    provider = FakeProvider()
    register(provider)
    conn = ProviderConnection(
        provider="fake",
        external_id="item-1",
        institution_name="Fake Bank",
        encrypted_credentials=encrypt_credentials({"access_token": "access-sandbox-SECRET-TOKEN-123"}),
    )
    db.add(conn)
    db.commit()
    return provider, conn


def _ptxn(ext, date, amount, payee, pending=False, pending_id=None, account="acc-chq"):
    return ProviderTransaction(ext, account, dt.date.fromisoformat(date), amount, payee, is_pending=pending, pending_external_id=pending_id)


# --- 1. Importing the same CSV twice creates no duplicate transactions ---------------


def test_csv_reimport_creates_no_duplicates(db, today):
    acct = account(db, "RBC Chequing", source="csv")
    first = _import(db, acct)
    assert first.inserted == 4  # the two identical Tim Hortons rows are both real
    second = _import(db, acct)
    assert second.inserted == 0 and second.duplicates == 4
    assert count(db) == 4


def test_csv_overlapping_file_only_adds_new_rows(db, today):
    acct = account(db, source="csv")
    _import(db, acct)
    more = RBC_CSV + 'Chequing,01234-5678901,10/12/2026,,"NETFLIX.COM",,-18.99,\n'
    result = _import(db, acct, more)
    assert result.inserted == 1 and count(db) == 5


def test_csv_import_via_http(auth_client, today):
    c = auth_client
    r = c.post("/accounts/new", data={"name": "RBC Chequing", "institution": "RBC", "type": "chequing", "source": "csv", "csrf_token": c.csrf})
    assert r.status_code == 200
    from app.db import SessionLocal
    from app.models import Account

    s = SessionLocal()
    acct_id = s.scalar(select(Account.id))
    s.close()
    for attempt in range(2):
        r = c.post("/import/upload", data={"account_id": acct_id, "csrf_token": c.csrf}, files={"file": ("rbc.csv", RBC_CSV.encode())})
        assert r.status_code == 200 and "Map columns" in r.text
        token = r.text.split('name="token" value="')[1].split('"')[0]
        if attempt == 1:
            assert "Using the saved mapping for RBC" in r.text
        form = {
            "csrf_token": c.csrf, "token": token, "account_id": acct_id, "action": "import",
            "date_col": "Transaction Date", "description_cols": ["Description 1", "Description 2"],
            "amount_mode": "single", "amount_col": "CAD$", "date_format": "auto", "has_header": "1",
            "institution": "RBC", "save_mapping": "1", "skip_rows": "0",
        }
        r = c.post("/import/map", data=form)
        assert r.status_code == 200
        assert ("Imported 4 new" if attempt == 0 else "Imported 0 new transactions") in r.text
    s = SessionLocal()
    assert count(s) == 4
    s.close()


# --- 2. A transaction arriving from both Plaid and CSV appears once -------------------


def test_synced_then_csv_appears_once(db, fake, today):
    provider, conn = fake
    provider.pages = [TransactionPage(added=[_ptxn("p1", "2026-10-02", -8412, "Loblaws")])]
    assert sync.sync_connection(db, conn).ok
    acct = db.scalar(select(Transaction)).account
    _import(db, acct)
    loblaws = [t for t in db.scalars(select(Transaction)) if t.amount == -8412]
    assert len(loblaws) == 1 and loblaws[0].external_id == "p1" and loblaws[0].import_fingerprint
    assert count(db) == 4
    _import(db, acct)  # and re-importing is still a no-op
    assert count(db) == 4


def test_csv_then_synced_appears_once(db, fake, today):
    provider, conn = fake
    assert sync.sync_connection(db, conn).ok  # creates the account
    acct = db.scalar(select(ProviderConnection)).accounts[0]
    _import(db, acct)
    t = next(t for t in db.scalars(select(Transaction)) if t.amount == -8412)
    t.category_id, t.category_status = cat(db, "Groceries").id, "confirmed"
    db.commit()
    provider.pages = [TransactionPage(added=[_ptxn("p1", "2026-10-03", -8412, "LOBLAWS 1234")])]
    assert sync.sync_connection(db, conn).ok
    loblaws = [t for t in db.scalars(select(Transaction)) if t.amount == -8412]
    assert len(loblaws) == 1
    assert loblaws[0].external_id == "p1" and loblaws[0].category_status == "confirmed"


def test_pending_replaced_by_posted_no_double_count(db, fake, today):
    provider, conn = fake
    provider.pages = [
        TransactionPage(added=[_ptxn("pend-1", "2026-10-05", -4000, "Pizza Place", pending=True)]),
        TransactionPage(added=[_ptxn("post-1", "2026-10-06", -4600, "Pizza Place", pending_id="pend-1")], removed=["pend-1"]),
    ]
    sync.sync_connection(db, conn)
    t = db.scalar(select(Transaction))
    categorize.confirm(t, cat(db, "Dining Out").id)
    db.commit()
    assert month_summary(db, "2026-10").row_for(cat(db, "Dining Out").id).spent == 4000
    sync.sync_connection(db, conn)
    rows = db.scalars(select(Transaction)).all()
    assert len(rows) == 1
    assert rows[0].external_id == "post-1" and not rows[0].is_pending and rows[0].amount == -4600
    assert rows[0].category_status == "confirmed"  # the user's choice survived posting
    assert month_summary(db, "2026-10").row_for(cat(db, "Dining Out").id).spent == 4600


def test_sync_uses_cursor(db, fake, today):
    provider, conn = fake
    provider.pages = [TransactionPage(added=[_ptxn("a", "2026-10-01", -100, "X")], next_cursor="c1")]
    sync.sync_connection(db, conn)
    sync.sync_connection(db, conn)
    assert provider.seen_cursors == [None, "c1"]


# --- 3. Credit card payments and own-account transfers don't count as spending --------


def test_transfers_excluded_from_spending(db, today):
    chq = account(db, "Chequing")
    visa = account(db, "Visa", "credit")
    sav = account(db, "Savings", "savings")
    groceries = cat(db, "Groceries")
    new = [
        txn(db, chq, "2026-10-03", -10000, "Loblaws", groceries),
        txn(db, chq, "2026-10-10", -50000, "PAYMENT - RBC VISA", confirmed=False),
        txn(db, visa, "2026-10-12", 50000, "PAYMENT THANK YOU", confirmed=False),
        txn(db, chq, "2026-10-15", -20000, "WWW TRF DDA", confirmed=False),
        txn(db, sav, "2026-10-15", 20000, "Transfer in", confirmed=False),
    ]
    after_ingest(db, new)
    db.commit()
    flags = {t.payee_raw: t.is_transfer for t in db.scalars(select(Transaction))}
    assert flags == {"Loblaws": False, "PAYMENT - RBC VISA": True, "PAYMENT THANK YOU": True, "WWW TRF DDA": True, "Transfer in": True}
    s = month_summary(db, "2026-10")
    assert s.total_spent == 10000 and s.income == 0
    assert s.uncategorized is None


def test_manual_transfer_override_wins(db, today):
    from app.services.transfers import set_transfer

    chq, sav = account(db, "Chequing"), account(db, "Savings", "savings")
    a = txn(db, chq, "2026-10-01", -3000, "Friend", confirmed=False)
    b = txn(db, sav, "2026-10-01", 3000, "Refund", confirmed=False)
    after_ingest(db, [a, b])
    assert a.is_transfer and b.is_transfer
    set_transfer(db, a, False)
    after_ingest(db, [])
    assert not a.is_transfer and not b.is_transfer  # detection doesn't re-pair manual choices


def test_rule_can_mark_transfer(db, today):
    from app.models import PayeeRule

    chq = account(db)
    db.add(PayeeRule(match_type="contains", match_value="mortgage pmt", mark_transfer=True))
    t = txn(db, chq, "2026-10-01", -150000, "MORTGAGE PMT 123", confirmed=False)
    after_ingest(db, [t])
    assert t.is_transfer and t.transfer_source == "rule"


# --- 4. Three paycheques in a month doesn't change spending math ----------------------


def test_three_paycheque_month(db, today):
    chq = account(db)
    pay, groceries = cat(db, "Paycheque"), cat(db, "Groceries")
    groceries.monthly_limit = 60000
    for month, paydays in (("2026-09", ["2026-09-04", "2026-09-18"]), ("2026-10", ["2026-10-02", "2026-10-16", "2026-10-30"])):
        for d in paydays:
            txn(db, chq, d, 245000, "PAYROLL", pay)
        txn(db, chq, f"{month}-05", -45000, "Loblaws", groceries)
    db.commit()
    sep, octo = month_summary(db, "2026-09"), month_summary(db, "2026-10")
    assert sep.income == 490000 and octo.income == 735000
    for s in (sep, octo):
        row = s.row_for(groceries.id)
        assert (row.spent, row.limit, row.remaining, row.pct) == (45000, 60000, 15000, 75)
        assert s.total_spent == 45000 and s.total_limit == 60000


def test_refund_reduces_category_spending(db, today):
    chq, shopping = account(db), cat(db, "Shopping")
    txn(db, chq, "2026-10-01", -10000, "Store", shopping)
    txn(db, chq, "2026-10-04", 2500, "Store refund", shopping)
    assert month_summary(db, "2026-10").row_for(shopping.id).spent == 7500


# --- 5. "Always" makes the next matching transaction auto-suggest the category --------


def test_always_rule_suggests_next_transaction(auth_client, today):
    from app.db import SessionLocal

    s = SessionLocal()
    chq = account(s)
    first = txn(s, chq, "2026-10-01", -1899, "NETFLIX.COM 866-579", confirmed=False)
    s.commit()
    subs = cat(s, "Subscriptions")
    r = auth_client.post(f"/api/transactions/{first.id}/category", json={"category_id": subs.id, "always": True},
                         headers={"X-CSRF-Token": auth_client.csrf})
    assert r.json()["rule_created"]
    # A brand-new transaction for the same payee (manual entry, CSV or sync all use the same pipeline).
    r = auth_client.post("/transactions/new", data={"date": "2026-11-01", "account_id": chq.id, "amount": "18.99",
                                                    "direction": "out", "payee": "NETFLIX.COM 866-579", "csrf_token": auth_client.csrf})
    assert r.status_code == 200
    s.expire_all()
    nxt = s.scalars(select(Transaction).order_by(Transaction.id.desc())).first()
    assert nxt.id != first.id
    assert (nxt.category_id, nxt.category_status, nxt.suggestion_source) == (subs.id, "suggested", "rule")
    s.close()


def test_suggestion_order_rule_then_history_then_provider(db, today):
    from app.models import PayeeRule

    chq = account(db)
    dining, groceries, shopping = cat(db, "Dining Out"), cat(db, "Groceries"), cat(db, "Shopping")
    # provider only
    t1 = txn(db, chq, "2026-10-01", -500, "Some Cafe", confirmed=False, provider_category="FOOD_AND_DRINK_COFFEE")
    categorize.suggest(db, t1)
    assert (t1.category_id, t1.suggestion_source) == (dining.id, "provider")
    # history beats provider
    txn(db, chq, "2026-09-01", -500, "Some Cafe", groceries)
    categorize.suggest(db, t1)
    assert (t1.category_id, t1.suggestion_source) == (groceries.id, "history")
    # rule beats history
    db.add(PayeeRule(match_type="contains", match_value="cafe", category_id=shopping.id))
    db.flush()
    categorize.suggest(db, t1)
    assert (t1.category_id, t1.suggestion_source) == (shopping.id, "rule")


def test_review_bulk_confirm(auth_client, today):
    from app.db import SessionLocal

    s = SessionLocal()
    chq = account(s)
    g = cat(s, "Groceries")
    ids = [txn(s, chq, "2026-10-0%d" % i, -100 * i, f"Shop {i}", confirmed=False).id for i in range(1, 4)]
    s.commit()
    data = {"csrf_token": auth_client.csrf, "action": "all", **{f"cat_{i}": str(g.id) for i in ids[:2]}, f"cat_{ids[2]}": ""}
    r = auth_client.post("/review", data=data)
    assert "Confirmed 2 transactions" in r.text
    s.expire_all()
    statuses = [s.get(Transaction, i).category_status for i in ids]
    assert statuses == ["confirmed", "confirmed", "suggested"]
    s.close()


# --- 6. Crossing 80% and 100% raises exactly one alert at each threshold per month -----


def test_alerts_fire_once_per_threshold_per_month(db, today):
    chq, dining = account(db), cat(db, "Dining Out")
    dining.monthly_limit = 10000

    def spend(date, cents):
        t = txn(db, chq, date, -cents, "Restaurant", dining)
        after_ingest(db, [t])
        db.commit()

    spend("2026-10-01", 5000)
    assert db.scalars(select(Alert)).all() == []
    spend("2026-10-02", 3500)  # 85%
    spend("2026-10-03", 500)  # 90%: no repeat
    assert [(a.threshold, a.month) for a in db.scalars(select(Alert))] == [(80, "2026-10")]
    spend("2026-10-04", 1500)  # 105%
    spend("2026-10-05", 5000)  # 155%: no repeat
    alerts.evaluate_alerts(db)
    assert sorted(a.threshold for a in db.scalars(select(Alert))) == [80, 100]
    # Refund below 80% and cross again: still no repeats this month.
    t = txn(db, chq, "2026-10-06", 9000, "Refund", dining)
    after_ingest(db, [t])
    spend("2026-10-07", 9000)
    assert len(db.scalars(select(Alert)).all()) == 2
    # Next month starts fresh.
    spend("2026-11-01", 9000)
    alerts.evaluate_alerts(db, "2026-11")
    assert sorted((a.month, a.threshold) for a in db.scalars(select(Alert))) == [
        ("2026-10", 80), ("2026-10", 100), ("2026-11", 80)]


def test_per_category_thresholds(db, today):
    chq, travel = account(db), cat(db, "Travel")
    travel.monthly_limit, travel.alert_thresholds = 10000, "50"
    t = txn(db, chq, "2026-10-01", -6000, "Air Canada", travel)
    after_ingest(db, [t])
    assert [a.threshold for a in db.scalars(select(Alert))] == [50]


def test_alert_banner_and_badge(auth_client, today):
    from app.db import SessionLocal

    s = SessionLocal()
    chq, dining = account(s), cat(s, "Dining Out")
    dining.monthly_limit = 1000
    t = txn(s, chq, "2026-10-01", -1200, "Restaurant", dining)
    after_ingest(s, [t])
    s.commit()
    s.close()
    html = auth_client.get("/").text
    assert "reached 100% of its" in html and "reached 80% of its" in html
    assert 'class="badge alert"' in html


# --- 7. Breaking Plaid doesn't stop CSV or manual workflows --------------------------


def test_broken_sync_does_not_block_csv_or_manual(auth_client, db, fake, today):
    provider, conn = fake
    provider.fail = ProviderAuthError("ITEM_LOGIN_REQUIRED: the login details have changed", code="ITEM_LOGIN_REQUIRED")
    report = sync.sync_connection(db, conn)
    assert not report.ok
    db.refresh(conn)
    assert conn.status == "login_required" and conn.needs_reconnect
    # Manual entry and CSV import still work.
    acct = account(db, "Wealthsimple Cash", source="csv")
    db.commit()
    _import(db, acct)
    r = auth_client.post("/transactions/new", data={"date": "2026-10-05", "account_id": acct.id, "amount": "12.00",
                                                    "direction": "out", "payee": "Cash", "csrf_token": auth_client.csrf})
    assert r.status_code == 200
    db.expire_all()
    assert count(db) == 5
    # And the reconnect prompt is visible.
    html = auth_client.get("/accounts").text
    assert "needs you to sign in again" in html and "Reconnect" in html


def test_generic_provider_error_recorded(db, fake, today):
    from app.providers.base import ProviderError

    provider, conn = fake
    provider.fail = ProviderError("INTERNAL_SERVER_ERROR: try later", code="INTERNAL_SERVER_ERROR")
    assert not sync.sync_connection(db, conn).ok
    db.refresh(conn)
    assert conn.status == "error"


# --- 8. Full data export (CSV and JSON) from Settings ---------------------------------


def test_exports(auth_client, today):
    from app.db import SessionLocal

    s = SessionLocal()
    chq = account(s, "Chequing")
    txn(s, chq, "2026-10-01", -1234, "Loblaws", cat(s, "Groceries"))
    s.add(ProviderConnection(provider="plaid", encrypted_credentials=encrypt_credentials({"access_token": "access-production-XYZ"})))
    s.commit()
    s.close()
    settings_html = auth_client.get("/settings").text
    assert "/export/transactions.csv" in settings_html and "/export/data.json" in settings_html
    csv_text = auth_client.get("/export/transactions.csv").text
    assert "Loblaws" in csv_text and "-12.34" in csv_text and "Groceries" in csv_text
    r = auth_client.get("/export/data.json")
    data = json.loads(r.text)
    assert {"transactions", "accounts", "categories", "goals", "alerts", "payee_rules"} <= set(data["tables"])
    assert data["tables"]["transactions"][0]["amount"] == -1234
    assert "encrypted_credentials" not in r.text and "access-production" not in r.text


# --- 9. No secrets in logs, the repo, or plain-text database columns ------------------


def test_access_token_encrypted_at_rest_and_not_logged(db_env, db, fake, caplog, today):
    caplog.set_level(logging.DEBUG)
    provider, _ = fake
    conn = sync.complete_link(db, "fake", {"public_token": "public-xyz"})
    provider.pages = [TransactionPage(added=[_ptxn("t1", "2026-10-01", -100, "X")])]
    sync.sync_connection(db, conn)
    db.close()
    from app.db import reset_engine

    reset_engine()  # flush WAL so the file on disk is complete
    raw = b"".join(p.read_bytes() for p in db_env.iterdir() if p.name.startswith("test.db"))
    assert b"SECRET-TOKEN" not in raw and b"access-sandbox" not in raw
    assert "SECRET-TOKEN" not in caplog.text


def test_repo_has_no_secrets():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    env_example = (root / ".env.example").read_text()
    for line in env_example.splitlines():
        if line.startswith(("PLAID_SECRET=", "PLAID_CLIENT_ID=", "APP_PASSWORD=", "ENCRYPTION_KEY=")):
            assert line.split("=", 1)[1].strip() in {"", "change-me"}, line
    assert ".env" in (root / ".gitignore").read_text().splitlines()


def test_link_events_logged_without_secrets(auth_client, caplog):
    caplog.set_level(logging.INFO)
    r = auth_client.post(
        "/api/connections/link-event",
        json={"event_name": "ERROR", "error_code": "NO_ACCOUNTS", "institution_name": "RBC Royal Bank",
              "public_token": "public-production-SECRET", "access_token": "access-production-SECRET"},
        headers={"X-CSRF-Token": auth_client.csrf},
    )
    assert r.status_code == 200
    assert "error_code='NO_ACCOUNTS'" in caplog.text and "RBC Royal Bank" in caplog.text
    assert "SECRET" not in caplog.text
