from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.money import MoneyError, format_cents, parse_cents
from app.services import recurring
from app.services.csv_import import Mapping, guess_mapping, parse_rows, read_table
from app.services.goals import goal_progress
from app.services.payees import clean_payee
from tests.helpers import account, cat, txn


@pytest.mark.parametrize(
    "text,cents",
    [("12.34", 1234), ("-12.34", -1234), ("$1,234.56", 123456), ("(5.00)", -500), ("5.00-", -500),
     ("12 CR", 1200), ("12 DR", -1200), ("0.005", 1), ("−3.10", -310), ("CAD 7", 700)],
)
def test_parse_cents(text, cents):
    assert parse_cents(text) == cents


def test_parse_cents_rejects_junk():
    with pytest.raises(MoneyError):
        parse_cents("abc")


def test_format_cents():
    assert format_cents(123456) == "$1,234.56"
    assert format_cents(-5) == "−$0.05"
    assert format_cents(500, signed=True) == "+$5.00"


@pytest.mark.parametrize(
    "raw,clean",
    [("LOBLAWS #1234 TORONTO ON", "Loblaws Toronto"), ("POS PURCHASE TIM HORTONS #0099", "Tim Hortons"),
     ("SQ *BLUE BOTTLE", "Blue Bottle"), ("Netflix.com", "Netflix.com"), ("", "")],
)
def test_clean_payee(raw, clean):
    assert clean_payee(raw) == clean


def test_csv_debit_credit_columns_and_date_detection():
    text = "Date,Description,Debit,Credit,Balance\n02/10/2026,Coffee,4.50,,995.50\n03/10/2026,Refund,,10.00,1005.50\n"
    header, _ = read_table(text)
    m = guess_mapping(header)
    assert (m.date_col, m.debit_col, m.credit_col, m.balance_col) == ("Date", "Debit", "Credit", "Balance")
    m.date_format = "%d/%m/%Y"
    rows = parse_rows(text, m).rows
    assert [(r.date, r.amount, r.balance) for r in rows] == [
        (dt.date(2026, 10, 2), -450, 99550), (dt.date(2026, 10, 3), 1000, 100550)]


def test_csv_credit_card_inverted_and_bad_rows():
    text = "Posted,Payee,Amount\n2026-10-01,Amazon,25.00\n2026-10-02,Payment,-500.00\nnot-a-date,Junk,1\n"
    m = Mapping(date_col="Posted", description_cols=["Payee"], amount_col="Amount", invert_amounts=True)
    res = parse_rows(text, m)
    assert [r.amount for r in res.rows] == [-2500, 50000]
    assert len(res.errors) == 1 and "Line 4" in res.errors[0]


def test_recurring_detection_and_flags(db):
    chq = account(db)
    subs = cat(db, "Subscriptions")
    for d in ["2026-06-12", "2026-07-12", "2026-08-12", "2026-09-12"]:
        txn(db, chq, d, -1899, "NETFLIX.COM", subs)
    for d in ["2026-07-03", "2026-08-21", "2026-09-02"]:  # irregular: not recurring
        txn(db, chq, d, -4000, "Random Shop")
    today = dt.date(2026, 10, 1)
    items = recurring.detect_recurring(db, today)
    assert [(i.name, i.frequency, i.amount, i.next_due) for i in items] == [
        ("Netflix.com", "monthly", -1899, dt.date(2026, 10, 12))]
    item = items[0]
    f = recurring.flags(item, today)
    assert f.new and not f.missed and not f.changed
    item.status = "confirmed"
    assert [o.date for o in recurring.upcoming(db, today, 30)] == [dt.date(2026, 10, 12)]
    # Missed: nothing seen by Oct 20.
    assert recurring.flags(item, dt.date(2026, 10, 20)).missed
    # Price change arrives.
    txn(db, chq, "2026-10-12", -2099, "NETFLIX.COM", subs)
    recurring.refresh_item(db, item, dt.date(2026, 10, 20))
    f = recurring.flags(item, dt.date(2026, 10, 20))
    assert not f.missed and f.changed and item.next_due == dt.date(2026, 11, 12)
    # Re-detection doesn't duplicate tracked payees.
    assert recurring.detect_recurring(db, dt.date(2026, 10, 20)) == []


def test_biweekly_paycheque_detected(db):
    chq = account(db)
    start = dt.date(2026, 7, 3)
    for i in range(7):
        txn(db, chq, start + dt.timedelta(weeks=2 * i), 245000, "PAYROLL ACME")
    items = recurring.detect_recurring(db, dt.date(2026, 10, 5))
    assert len(items) == 1 and items[0].frequency == "biweekly" and items[0].amount > 0


def test_debt_goal_projection(db):
    from app.models import Goal
    from app.services.balances import record_balance

    loan = account(db, "Car loan", "loan")
    today = dt.date(2026, 10, 1)
    record_balance(db, loan, 1_000_000, today - dt.timedelta(days=60))
    record_balance(db, loan, 900_000, today)
    goal = Goal(name="Car", type="debt", target_amount=1_200_000, accounts=[loan])
    db.add(goal)
    db.flush()
    p = goal_progress(db, goal, today)
    assert p.owed == 900_000 and p.progress == 300_000 and p.pct == 25
    assert p.pace_per_month == 50_000
    assert p.projected_date == today + dt.timedelta(days=541)  # 900000 / (100000/60) per day


def test_savings_goal_contributions(db):
    from app.models import Goal, GoalContribution

    today = dt.date(2026, 10, 1)
    goal = Goal(name="Trip", type="savings", target_amount=300_000, progress_source="contributions",
                target_date=dt.date(2027, 6, 1))
    goal.contributions = [GoalContribution(date=today - dt.timedelta(days=d), amount=50_000) for d in (60, 30, 0)]
    db.add(goal)
    db.flush()
    p = goal_progress(db, goal, today)
    assert p.progress == 150_000 and p.remaining == 150_000
    assert p.projected_date is not None and p.on_track is True


# --- PlaidProvider against a mocked Plaid API ---------------------------------------


def _plaid_transport(responses: dict, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        result = responses[request.url.path]
        if callable(result):
            result = result(body)
        status = 400 if "error_code" in result else 200
        return httpx.Response(status, json=result)

    return httpx.MockTransport(handler)


@pytest.fixture()
def plaid_env(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "psecret")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_plaid_provider_maps_data(plaid_env):
    from app.providers.plaid import PlaidProvider

    pages = iter([
        {"added": [{"transaction_id": "t1", "account_id": "a1", "amount": 12.5, "date": "2026-10-01", "name": "TIM HORTONS",
                    "merchant_name": "Tim Hortons", "pending": False, "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE"}}],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": True},
        {"added": [{"transaction_id": "t2", "account_id": "a1", "amount": -2450.0, "date": "2026-10-02", "name": "PAYROLL", "pending": False,
                    "pending_transaction_id": None}],
         "modified": [], "removed": [{"transaction_id": "old"}], "next_cursor": "c2", "has_more": False},
    ])
    calls: list = []
    responses = {
        "/transactions/sync": lambda body: next(pages),
        "/accounts/get": {"accounts": [
            {"account_id": "a1", "name": "Chequing", "mask": "1234", "type": "depository", "subtype": "checking", "balances": {"current": 1500.25, "available": 1400}},
            {"account_id": "a2", "name": "TFSA", "type": "investment", "subtype": "tfsa", "balances": {"current": 20000}},
            {"account_id": "a3", "name": "Visa", "type": "credit", "subtype": "credit card", "balances": {"current": 310.4}}]},
        "/liabilities/get": {"liabilities": {"credit": [{"account_id": "a3", "aprs": [{"apr_type": "purchase_apr", "apr_percentage": 20.99}],
                                                         "minimum_payment_amount": 10, "next_payment_due_date": "2026-10-25"}]}},
        "/investments/holdings/get": {"error_code": "PRODUCTS_NOT_SUPPORTED", "error_message": "nope", "error_type": "ITEM_ERROR"},
    }
    p = PlaidProvider(transport=_plaid_transport(responses, calls))
    creds = {"access_token": "access-sandbox-abc"}
    page = p.fetch_transactions(creds, None)
    assert [(t.external_id, t.amount, t.payee, t.category) for t in page.added] == [
        ("t1", -1250, "Tim Hortons", "FOOD_AND_DRINK_COFFEE"), ("t2", 245000, "PAYROLL", None)]
    assert page.removed == ["old"] and page.next_cursor == "c2"
    assert calls[1][1]["cursor"] == "c1" and calls[0][1]["secret"] == "psecret"
    accts = p.fetch_accounts(creds)
    assert [(a.type, a.subtype) for a in accts] == [("chequing", "checking"), ("registered", "TFSA"), ("credit", "credit card")]
    assert [b.current for b in p.fetch_balances(creds)] == [150025, 2000000, 31040]
    lib = p.fetch_liabilities(creds)[0]
    assert (str(lib.interest_rate), lib.min_payment, lib.next_payment_due) == ("20.99", 1000, dt.date(2026, 10, 25))
    assert p.fetch_holdings(creds) == []  # unsupported product is not an error


def test_plaid_login_required_maps_to_auth_error(plaid_env):
    from app.providers.base import ProviderAuthError
    from app.providers.plaid import PlaidProvider

    calls: list = []
    p = PlaidProvider(transport=_plaid_transport(
        {"/accounts/get": {"error_code": "ITEM_LOGIN_REQUIRED", "error_type": "ITEM_ERROR", "error_message": "login required"}}, calls))
    with pytest.raises(ProviderAuthError):
        p.fetch_accounts({"access_token": "x"})


def test_plaid_restarts_on_mutation_during_pagination(plaid_env):
    from app.providers.plaid import PlaidProvider

    seq = iter([
        {"added": [], "modified": [], "removed": [], "next_cursor": "c1", "has_more": True},
        {"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "error_type": "TRANSACTIONS_ERROR", "error_message": "x"},
        {"added": [], "modified": [], "removed": [], "next_cursor": "c9", "has_more": False},
    ])
    calls: list = []
    p = PlaidProvider(transport=_plaid_transport({"/transactions/sync": lambda b: next(seq)}, calls))
    page = p.fetch_transactions({"access_token": "x"}, "c0")
    assert page.next_cursor == "c9"
    assert [c[1].get("cursor") for c in calls] == ["c0", "c1", "c0"]


def test_plaid_link_token_request(plaid_env):
    from app.providers.plaid import PlaidProvider

    calls: list = []
    p = PlaidProvider(transport=_plaid_transport({"/link/token/create": {"link_token": "link-123"}}, calls))
    assert p.link() == {"kind": "plaid_link", "link_token": "link-123"}
    body = calls[0][1]
    assert body["products"] == ["transactions"] and body["country_codes"] == ["CA"]
    assert body["optional_products"] == ["liabilities", "investments"]
    p.link({"access_token": "access-1"})  # update mode for reconnect
    assert calls[1][1]["access_token"] == "access-1" and "products" not in calls[1][1]
