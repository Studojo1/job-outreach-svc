"""Gmail OAuth tokens are encrypted by the application before they are stored.

Privacy Policy v2.0: the database is encrypted at rest by the provider, and
the credentials that let us act for a user are additionally encrypted by us.
LinkedIn cookies always were; Gmail access and refresh tokens were plaintext
in email_accounts until now.

Format: "enc:v1:<nonce_b64>:<ciphertext_b64>", AES-256-GCM with the existing
LINKEDIN_ENCRYPTION_KEY (services/linkedin_outreach/crypto.py) and a fresh
nonce per value. The prefix makes encrypted vs legacy plaintext unambiguous:
a Google token never starts with "enc:".

The only two entry points are encrypt_token and decrypt_token. The
EmailAccount.access_token / refresh_token columns use EncryptedToken, which
calls them on every ORM write and read, so no code path can store a token in
the clear by assigning the attribute. Raw SQL that reads these columns must
call decrypt_token itself (services/account_deletion.py does).

Rows written before this change are still plaintext. Reads accept them, and
backfill_encrypt_gmail_tokens (run at startup and by
scripts/encrypt_gmail_tokens.py) rewrites them encrypted.
"""
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.types import Text, TypeDecorator

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"


class TokenDecryptError(RuntimeError):
    """A value carries the encrypted prefix but does not decrypt with our key."""


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt_token(value: Optional[str]) -> Optional[str]:
    """Plaintext token -> "enc:v1:..." string. None and "" (no token) pass
    through; an already-encrypted value is returned as is."""
    if not value or is_encrypted(value):
        return value
    from services.linkedin_outreach.crypto import encrypt
    ct_b64, nonce_b64 = encrypt(value)
    return f"{PREFIX}{nonce_b64}:{ct_b64}"


def decrypt_token(value: Optional[str]) -> Optional[str]:
    """Stored value -> plaintext token. Legacy plaintext rows are returned
    unchanged so the deploy is safe before the backfill has run."""
    if not is_encrypted(value):
        return value
    from services.linkedin_outreach.crypto import decrypt
    try:
        nonce_b64, ct_b64 = value[len(PREFIX):].split(":", 1)
        return decrypt(ct_b64, nonce_b64)
    except Exception as e:
        # Never include the value: it is a secret even when we cannot read it.
        raise TokenDecryptError("Stored Gmail token could not be decrypted (wrong LINKEDIN_ENCRYPTION_KEY?)") from e


class EncryptedToken(TypeDecorator):
    """Text column that holds encrypt_token() output."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_token(value)

    def process_result_value(self, value, dialect):
        return decrypt_token(value)


def backfill_encrypt_gmail_tokens(db) -> int:
    """Encrypt every plaintext token left in email_accounts. Returns the
    number of values rewritten; 0 on every run after the first.

    Raw SQL, so the ORM type does not hide which rows are still plaintext.
    Each UPDATE only applies if the column still holds the plaintext we read:
    a token refresh that lands in between keeps its new token instead of being
    overwritten with the old one. Safe to run from several replicas at once.
    """
    rows = db.execute(text(
        "SELECT id, access_token, refresh_token FROM email_accounts "
        "WHERE (access_token IS NOT NULL AND access_token <> '' AND access_token NOT LIKE 'enc:v1:%') "
        "OR (refresh_token IS NOT NULL AND refresh_token <> '' AND refresh_token NOT LIKE 'enc:v1:%')"
    )).fetchall()
    changed = 0
    for row_id, access, refresh in rows:
        for col, old in (("access_token", access), ("refresh_token", refresh)):
            if not old or is_encrypted(old):
                continue
            n = db.execute(
                text(f"UPDATE email_accounts SET {col} = :new WHERE id = :id AND {col} = :old"),  # noqa: S608 - col is one of two literals above
                {"new": encrypt_token(old), "id": row_id, "old": old},
            ).rowcount
            changed += n or 0
    db.commit()
    if changed:
        logger.info("[GMAIL_TOKENS] Encrypted %d plaintext token value(s) in %d row(s)", changed, len(rows))
    return changed


def run_startup_backfill() -> None:
    """Called once per process at app startup. Never raises: a failure here
    must not stop the service, and the next start tries again."""
    from core.config import settings
    if not settings.LINKEDIN_ENCRYPTION_KEY:
        logger.error("[GMAIL_TOKENS] LINKEDIN_ENCRYPTION_KEY is not set; plaintext Gmail tokens left as they are")
        return
    from database.session import SessionLocal
    db = SessionLocal()
    try:
        backfill_encrypt_gmail_tokens(db)
    except Exception:
        db.rollback()
        logger.exception("[GMAIL_TOKENS] Startup backfill failed; will retry on next start")
    finally:
        db.close()
