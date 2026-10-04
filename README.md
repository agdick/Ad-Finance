# Finance

A self-hosted, single-user personal finance app: monthly category limits, budget vs. actual
for the current calendar month, bank sync via Plaid, CSV import, and manual entry. CAD only.

## Run it

```sh
cp .env.example .env
# Fill in APP_PASSWORD, SECRET_KEY and ENCRYPTION_KEY (commands to generate them are in the file).
# Add PLAID_CLIENT_ID / PLAID_SECRET to enable bank sync (optional).
docker compose up -d --build
```

Open http://localhost:8000 (or `APP_PORT`). Database migrations run automatically at startup.
Data lives in the `finance-data` Docker volume (SQLite at `/data/finance.db`).

**Behind a reverse proxy with HTTPS:** set `COOKIE_SECURE=true`. The app trusts `X-Forwarded-*`
headers, so only publish it through your proxy if it's reachable from outside your home network.

**Keep a copy of `ENCRYPTION_KEY`.** It encrypts the stored Plaid access tokens. If you lose it,
the transactions are still there, but each bank connection has to be re-linked.

## Backups and export

- Consistent database snapshot (safe while running):
  `docker compose exec app python -m app.cli backup`, which writes to `/data/backups/`. Copy it off
  the volume with `docker compose cp app:/data/backups ./backups`.
- Settings → Export: transactions as CSV, or every table as JSON. Exports never include tokens.
- Restore: stop the app and replace `/data/finance.db` with a backup file.

## Plaid setup (verify before relying on it)

1. Sign up at Plaid and create a team. Pick **Personal use** to get the Trial plan
   (production data, max 10 Items). Check the current terms when you sign up.
2. In the dashboard, check that your plan includes **Transactions**. Liabilities and
   Investments are optional: the app requests them as optional products and silently skips them
   where they aren't available.
3. Put the client ID and the **production** secret in `.env` (`PLAID_ENV=production`), then restart.
4. Accounts → **Connect a bank**. See which of your institutions appear in Plaid Link. RBC is
   expected to work. If **Wealthsimple** isn't listed, use CSV import for it.

Sync runs every `SYNC_INTERVAL_HOURS` (default 6) and on **Sync now**. If a bank needs you to sign
in again, or access is about to expire, the account shows a **Reconnect** button. CSV import and
manual entry keep working regardless of Plaid's state.

## How the numbers work

- **Spending** = money out (net of refunds) in *spending* categories, for the calendar month.
  Transfers are excluded. Income categories (e.g. *Paycheque*) are reported separately, so a
  month with three paycheques budgets exactly like any other month.
- **Transfers.** An outflow in one account matched by an equal inflow in another account within
  4 days is flagged as a transfer automatically, which covers credit card payments and moves to
  savings. You can override this per transaction (your choice always wins), or add a payee rule
  with "Transfer" for payments to accounts you don't track.
- **Unconfirmed categories count** toward totals using their suggested category. Totals that
  include them are marked with an amber dot.
- **Suggestions** come from, in order: your payee rules, your history for that payee, then the
  bank's or CSV's own category.
- **Duplicates.** Re-importing a CSV only adds rows that weren't already there. A bank-synced
  transaction and a CSV or manual one for the same account and amount, within 3 days of each
  other, are merged into one. When a pending transaction posts, it's updated in place, so your
  category and note carry over.
- **Alerts** fire once per category per threshold per month (default 80% and 100%; configurable
  per category). They show as a banner and a badge on Home. Other channels plug in through
  `services/alerts.py::register_notifier`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
# Run locally (needs the same env vars as .env):
set -a; . ./.env; set +a; DATA_DIR=./data .venv/bin/alembic upgrade head
DATA_DIR=./data .venv/bin/uvicorn app.main:create_app --factory --reload
```

Layout:

| Path | What |
| --- | --- |
| `app/providers/base.py` | `SyncProvider` interface (`link`, `fetch_accounts`, `fetch_transactions(since)`, `fetch_balances`, …) |
| `app/providers/plaid.py` | `PlaidProvider` (REST via httpx; cursor-based `/transactions/sync`) |
| `app/services/sync.py` | The only code that calls providers; maps their data into local tables |
| `app/services/` | Budget, categorization, transfers, dedupe, CSV import, recurring, goals, alerts, export |
| `app/web/`, `app/templates/` | Server-rendered pages (FastAPI + Jinja), small vanilla JS in `app/static/app.js` |
| `migrations/` | Alembic migrations (`alembic revision --autogenerate -m "..."` after model changes) |

**Adding another aggregator:** implement `SyncProvider`, `register()` it in
`app/providers/__init__.py`, and if its link flow uses a browser widget, add a handler keyed by
the `kind` its `link()` returns in `static/app.js`. Budget logic, categorization and the UI don't
change.

**Splitting transactions (not built):** budget math reads transactions only through
`services/budget.py::spending_lines()`. A future `transaction_splits` table only needs to be
read there.

Money is always integer cents. Floats are never used for amounts.
