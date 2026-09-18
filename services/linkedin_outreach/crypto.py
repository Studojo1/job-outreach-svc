"""AES-256-GCM encryption for LinkedIn session tokens."""

import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_key() -> bytes:
    from core.config import settings
    key_b64 = settings.LINKEDIN_ENCRYPTION_KEY
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise ValueError("LINKEDIN_ENCRYPTION_KEY must be 32 bytes (base64-encoded 256-bit key)")
    return key


def encrypt(plaintext: str) -> tuple[str, str]:
    """Encrypt a single value. Returns (ciphertext_b64, nonce_b64)."""
    key = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(ct).decode(), base64.b64encode(nonce).decode()


def encrypt_pair(val1: str, val2: str) -> tuple[str, str, str]:
    """Encrypt two values each with an independent nonce.

    Returns (enc1_b64, enc2_b64, nonce_b64) where nonce_b64 is the concatenated
    nonces (24 bytes base64) so the DB schema (single nonce column) is unchanged.

    Using separate nonces prevents AES-GCM nonce-reuse: encrypting two plaintexts
    with the same (key, nonce) leaks their XOR, which could expose li_at if
    jsessionid is already known.
    """
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce1 = os.urandom(12)
    nonce2 = os.urandom(12)
    ct1 = aesgcm.encrypt(nonce1, val1.encode(), None)
    ct2 = aesgcm.encrypt(nonce2, val2.encode(), None)
    # Store both nonces concatenated (24 bytes) in the single nonce column
    combined_nonce = base64.b64encode(nonce1 + nonce2).decode()
    return (
        base64.b64encode(ct1).decode(),
        base64.b64encode(ct2).decode(),
        combined_nonce,
    )


def decrypt(ciphertext_b64: str, nonce_b64: str) -> str:
    """Decrypt a value encrypted with encrypt() or the first value from encrypt_pair()."""
    key = _get_key()
    raw_nonce = base64.b64decode(nonce_b64)
    # Support both legacy (12-byte) and new (24-byte combined) nonce formats
    nonce = raw_nonce[:12]
    ct = base64.b64decode(ciphertext_b64)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


def decrypt_second(ciphertext_b64: str, nonce_b64: str) -> str:
    """Decrypt the second value from encrypt_pair() (uses bytes 12–24 of the nonce)."""
    key = _get_key()
    raw_nonce = base64.b64decode(nonce_b64)
    if len(raw_nonce) < 24:
        # Legacy record encrypted with shared nonce — fall back gracefully
        nonce = raw_nonce[:12]
    else:
        nonce = raw_nonce[12:24]
    ct = base64.b64decode(ciphertext_b64)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()
