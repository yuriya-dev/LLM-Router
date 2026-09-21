# src/core/key_cache.py
"""
In-memory key pool cache per provider.

Problem solved:
  pool_manager.get_healthy_keys() previously issued a Supabase query (+ a DB
  write via restore_all_expired_cooldowns) on *every* incoming request.
  At 100 req/s this becomes a significant bottleneck and wastes Supabase quota.

Solution:
  - Results of get_healthy_keys() are cached in memory per provider with a
    configurable TTL (default 30 s, set via KEY_CACHE_TTL in .env).
  - The cache is invalidated immediately whenever a key is marked cooldown or
    dead, so the next request for that provider gets fresh data.
  - restore_all_expired_cooldowns() now runs as a background periodic task
    (every COOLDOWN_RESTORE_INTERVAL seconds) rather than per-request.
"""
import logging
import time
from typing import Dict, List, Any, Optional

from src.core.logging_config import get_logger

logger = get_logger(__name__)


class KeyPoolCache:
    """Simple per-provider in-memory key cache with TTL expiry."""

    def __init__(self, ttl_seconds: int = 30):
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._loaded_at: Dict[str, float] = {}

    def get(self, provider: str) -> Optional[List[Dict[str, Any]]]:
        """Return cached keys if fresh, else None (triggers DB fetch)."""
        if provider not in self._cache:
            return None
        age = time.monotonic() - self._loaded_at.get(provider, 0.0)
        if age > self.ttl_seconds:
            logger.debug(
                f"[key_cache] Cache expired for provider={provider} age={age:.1f}s",
                extra={"event": "cache_miss", "provider": provider, "age_secs": round(age, 1)}
            )
            return None
        return self._cache[provider]

    def set(self, provider: str, keys: List[Dict[str, Any]]) -> None:
        self._cache[provider] = keys
        self._loaded_at[provider] = time.monotonic()
        logger.debug(
            f"[key_cache] Cached {len(keys)} keys for provider={provider}",
            extra={"event": "cache_set", "provider": provider, "key_count": len(keys)}
        )

    def invalidate(self, provider: Optional[str] = None) -> None:
        """Invalidate one provider or all providers."""
        if provider:
            removed = self._cache.pop(provider, None)
            self._loaded_at.pop(provider, None)
            if removed is not None:
                logger.debug(
                    f"[key_cache] Invalidated cache for provider={provider}",
                    extra={"event": "cache_invalidated", "provider": provider}
                )
        else:
            self._cache.clear()
            self._loaded_at.clear()
            logger.debug(
                "[key_cache] Full cache invalidated",
                extra={"event": "cache_invalidated_all"}
            )


# ── Application-wide singleton — initialised in main.py lifespan ─────────────
_cache_instance: Optional[KeyPoolCache] = None


def init_key_cache(ttl_seconds: int = 30) -> KeyPoolCache:
    global _cache_instance
    _cache_instance = KeyPoolCache(ttl_seconds=ttl_seconds)
    logger.info(f"[key_cache] Initialised with TTL={ttl_seconds}s",
                extra={"event": "cache_init", "ttl_seconds": ttl_seconds})
    return _cache_instance


def get_key_cache() -> KeyPoolCache:
    if _cache_instance is None:
        raise RuntimeError("Key cache not initialised. Call init_key_cache() at startup.")
    return _cache_instance
