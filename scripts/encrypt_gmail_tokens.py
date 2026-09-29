"""Encrypt Gmail OAuth tokens still stored in plaintext in email_accounts.

The service also does this on every start (services/gmail_tokens.py); this
script is for running it by hand and seeing the count. Idempotent: a second
run changes nothing.

Usage:
    python -m scripts.encrypt_gmail_tokens            # count plaintext values only
    python -m scripts.encrypt_gmail_tokens --apply    # encrypt them
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from database.session import SessionLocal  # noqa: E402
from services.gmail_tokens import backfill_encrypt_gmail_tokens  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        if not args.apply:
            n = db.execute(text(
                "SELECT count(*) FILTER (WHERE access_token <> '' AND access_token NOT LIKE 'enc:v1:%') + "
                "count(*) FILTER (WHERE refresh_token <> '' AND refresh_token NOT LIKE 'enc:v1:%') "
                "FROM email_accounts"
            )).scalar()
            print(f"{n} plaintext token value(s). Re-run with --apply to encrypt.")
            return
        print(f"Encrypted {backfill_encrypt_gmail_tokens(db)} token value(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
