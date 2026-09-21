# src/core/crypto.py
"""
Symmetric encryption / decryption for provider API keys stored in Supabase.

Algorithm: Fernet (AES-128-CBC + HMAC-SHA256) via the `cryptography` package.

Why encrypt at rest?
  Even if someone gains read access to the Supabase `provider_keys` table
  (e.g. via a leaked service-role key or misconfigured RLS policy), they
  cannot use the provider API keys without the ENCRYPTION_KEY secret.

Backward compatibility:
  If ENCRYPTION_KEY is not configured the module is a transparent pass-through
  (encrypt returns the key unchanged; decrypt returns the stored value as-is).
  Keys stored in plaintext before encryption was enabled are also handled
  gracefully — decrypt() falls back to plaintext on any decryption error.

Usage:
    from src.core.crypto import encrypt_key, decrypt_key
    stored = encrypt_key("sk-live-abc123")
    original = decrypt_key(stored)   # "sk-live-abc123"

Generating a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import logging
from typing import Optional

from src.core.logging_config import get_logger

logger = get_logger(__name__)

_fernet = None  # Lazy-initialised singleton


def _get_fernet():
    """Lazily initialise the Fernet cipher from ENCRYPTION_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from src.config import settings
        if not settings.ENCRYPTION_KEY:
            return None
        from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
        _fernet = Fernet(settings.ENCRYPTION_KEY.encode())
        logger.info("[crypto] Fernet cipher initialised — keys will be encrypted at rest")
        return _fernet
    except Exception:
        logger.warning("[crypto] Failed to initialise Fernet cipher; running in plaintext mode",
                       exc_info=True)
        return None


def encrypt_key(api_key: str) -> str:
    """
    Encrypt an API key before persisting it to Supabase.
    Returns the plaintext unchanged if ENCRYPTION_KEY is not configured.
    """
    f = _get_fernet()
    if not f:
        return api_key
    return f.encrypt(api_key.encode()).decode()


def decrypt_key(stored_value: str) -> str:
    """
    Decrypt a stored key value retrieved from Supabase.

    Falls back to the raw value if:
      - ENCRYPTION_KEY is not set (plaintext mode)
      - Decryption fails (key was stored before encryption was enabled)
    """
    f = _get_fernet()
    if not f:
        return stored_value
    try:
        return f.decrypt(stored_value.encode()).decode()
    except Exception:
        # Migration mode: key was inserted before encryption was turned on
        logger.debug("[crypto] Decryption failed; returning value as plaintext (migration mode)")
        return stored_value
