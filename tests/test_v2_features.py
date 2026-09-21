# tests/test_v2_features.py
"""
Unit tests for v2 features:
  - Circuit Breaker state machine (CLOSED -> OPEN -> HALF-OPEN -> CLOSED)
  - Crypto (Fernet encryption/decryption with plaintext fallback)
  - KeyCache TTL and invalidation
"""
import time
import pytest
from unittest.mock import MagicMock, patch

import sys
sys.modules.setdefault("supabase", MagicMock())

from src.core.circuit_breaker import CircuitBreaker
from src.core.crypto import encrypt_key, decrypt_key
from src.core.key_cache import KeyPoolCache


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_secs=10.0)
        assert cb.is_open("gemini") is False

    def test_trips_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_secs=10.0)
        cb.record_failure("groq")
        cb.record_failure("groq")
        assert cb.is_open("groq") is False
        cb.record_failure("groq")
        assert cb.is_open("groq") is True

    def test_half_open_recovery(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_secs=0.1)
        cb.record_failure("mistral")
        cb.record_failure("mistral")
        assert cb.is_open("mistral") is True
        time.sleep(0.15)
        # Should transition to HALF-OPEN (is_open returns False)
        assert cb.is_open("mistral") is False

    def test_success_resets_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_secs=10.0)
        cb.record_failure("openai")
        cb.record_failure("openai")
        assert cb.is_open("openai") is True
        cb.record_success("openai")
        assert cb.is_open("openai") is False

    def test_manual_reset(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_secs=100.0)
        cb.record_failure("dashscope")
        assert cb.is_open("dashscope") is True
        assert cb.reset("dashscope") is True
        assert cb.is_open("dashscope") is False


class TestCrypto:
    def test_plaintext_fallback_when_no_key(self):
        with patch("src.config.settings.ENCRYPTION_KEY", None):
            raw = "sk-live-1234567890"
            encrypted = encrypt_key(raw)
            assert encrypted == raw
            decrypted = decrypt_key(encrypted)
            assert decrypted == raw

    def test_fernet_encryption_decryption(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        with patch("src.config.settings.ENCRYPTION_KEY", key):
            import src.core.crypto as crypto_module
            crypto_module._fernet = None  # reset cache
            raw = "sk-live-secret-api-key"
            encrypted = encrypt_key(raw)
            assert encrypted != raw
            decrypted = decrypt_key(encrypted)
            assert decrypted == raw
            crypto_module._fernet = None

    def test_graceful_fallback_for_unencrypted_stored_keys(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        with patch("src.config.settings.ENCRYPTION_KEY", key):
            import src.core.crypto as crypto_module
            crypto_module._fernet = None
            raw_unencrypted = "sk-old-unencrypted-key"
            # Decrypting an unencrypted key should fall back to returning it as-is
            result = decrypt_key(raw_unencrypted)
            assert result == raw_unencrypted
            crypto_module._fernet = None


class TestKeyPoolCache:
    def test_cache_miss_and_hit(self):
        cache = KeyPoolCache(ttl_seconds=10)
        assert cache.get("gemini") is None
        keys = [{"id": "1", "key_value": "abc"}]
        cache.set("gemini", keys)
        assert cache.get("gemini") == keys

    def test_cache_invalidation(self):
        cache = KeyPoolCache(ttl_seconds=10)
        cache.set("groq", [{"id": "2"}])
        cache.invalidate("groq")
        assert cache.get("groq") is None

    def test_cache_ttl_expiry(self):
        cache = KeyPoolCache(ttl_seconds=0.1)
        cache.set("openai", [{"id": "3"}])
        time.sleep(0.15)
        assert cache.get("openai") is None
