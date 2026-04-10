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
    """Encrypt plaintext string.

    Returns (ciphertext_b64, nonce_b64).
    Note: nonce is per-field, so both li_at and jsessionid share the same nonce
    but have different ciphertexts. Caller can use one nonce for both.
    """
    key = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(ct).decode(), base64.b64encode(nonce).decode()


def encrypt_pair(val1: str, val2: str) -> tuple[str, str, str]:
    """Encrypt two values with the same nonce.

    Returns (enc1_b64, enc2_b64, nonce_b64).
    """
    key = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct1 = aesgcm.encrypt(nonce, val1.encode(), None)
    ct2 = aesgcm.encrypt(nonce, val2.encode(), None)
    return (
        base64.b64encode(ct1).decode(),
        base64.b64encode(ct2).decode(),
        base64.b64encode(nonce).decode(),
    )


def decrypt(ciphertext_b64: str, nonce_b64: str) -> str:
    """Decrypt a ciphertext encrypted with encrypt() or encrypt_pair()."""
    key = _get_key()
    nonce = base64.b64decode(nonce_b64)
    ct = base64.b64decode(ciphertext_b64)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()
