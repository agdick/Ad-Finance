"""Maintenance commands, e.g. `docker compose exec app python -m app.cli backup`."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

from .config import get_settings
from .db import SessionLocal


def backup(dest_dir: str | None) -> str:
    s = get_settings()
    if not s.database_url.startswith("sqlite:///"):
        sys.exit("backup only supports the SQLite database; use your database's own tools.")
    src = s.database_url.split("///", 1)[1]
    dest_dir = dest_dir or os.path.join(s.data_dir, "backups")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"finance-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
        source.backup(target)  # consistent snapshot even while the app is running
    return dest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="Write a consistent copy of the SQLite database")
    b.add_argument("--dest", help="Directory (default: $DATA_DIR/backups)")
    sub.add_parser("sync", help="Run a bank sync now")
    sub.add_parser("gen-key", help="Print a new ENCRYPTION_KEY")
    args = parser.parse_args(argv)

    if args.cmd == "backup":
        print(backup(args.dest))
    elif args.cmd == "sync":
        from .services.sync import sync_all

        db = SessionLocal()
        try:
            for r in sync_all(db):
                print(f"connection {r.connection_id}: {'ok' if r.ok else 'FAILED'} +{r.added} ~{r.updated} -{r.removed} {r.message or ''}")
        finally:
            db.close()
    elif args.cmd == "gen-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())


if __name__ == "__main__":
    main()
