# src/core/pool_manager.py
import asyncio
import datetime
import hashlib
import secrets
from typing import List, Dict, Any, Optional

from supabase import create_client, Client

from src.config import settings
from src.core.crypto import decrypt_key
from src.core.key_cache import get_key_cache
from src.core.logging_config import get_logger

logger = get_logger(__name__)

# Shared Supabase client — also exported so router.py and admin.py can reuse it
supabase: Optional[Client] = (
    create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    if settings.SUPABASE_URL and settings.SUPABASE_KEY
    else None
)


# ── Cooldown Restoration ──────────────────────────────────────────────────────

async def restore_all_expired_cooldowns() -> None:
    """
    Restore keys whose cooldown_until has passed back to 'healthy'.

    IMPORTANT: This is now called by a periodic background task (every
    COOLDOWN_RESTORE_INTERVAL seconds), NOT on every request.  This removes
    the per-request DB write that was a major bottleneck at high traffic.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _restore():
        if not supabase:
            return
        try:
            result = supabase.table("provider_keys")\
                .update({"status": "healthy", "cooldown_until": None, "error_count": 0})\
                .eq("status", "cooldown")\
                .lt("cooldown_until", now)\
                .execute()
            restored = len(result.data or [])
            if restored > 0:
                logger.info(
                    f"[pool_manager] Restored {restored} expired cooldown keys",
                    extra={"event": "cooldown_restored", "count": restored}
                )
                # Keys changed → full cache invalidation so next requests get fresh data
                get_key_cache().invalidate()
        except Exception as e:
            logger.error(f"[pool_manager] Error restoring cooldown keys: {e}", exc_info=True)

    await asyncio.to_thread(_restore)


# ── Key Fetching (cached) ─────────────────────────────────────────────────────

async def get_healthy_keys(provider: str) -> List[Dict[str, Any]]:
    """
    Return healthy provider keys, using the in-memory cache when fresh.

    Keys are decrypted transparently before being returned so callers always
    receive the plaintext API key value regardless of storage mode.
    """
    cache = get_key_cache()
    cached = cache.get(provider)
    if cached is not None:
        return cached

    def _fetch():
        if not supabase:
            logger.warning("[pool_manager] SUPABASE_URL/KEY missing in env variables")
            return []
        try:
            return supabase.table("provider_keys")\
                .select("id, provider, key_name, key_value, priority")\
                .eq("provider", provider)\
                .eq("status", "healthy")\
                .order("priority", desc=False)\
                .order("last_used_at", nullsfirst=True)\
                .execute().data or []
        except Exception as e:
            logger.error(
                f"[pool_manager] Error fetching keys for provider={provider}: {e}",
                extra={"event": "key_fetch_error", "provider": provider},
                exc_info=True
            )
            return []

    raw_keys = await asyncio.to_thread(_fetch)

    # Decrypt key_value in-memory; never persist decrypted values
    decrypted = [
        {**k, "key_value": decrypt_key(k["key_value"])}
        for k in raw_keys
    ]

    cache.set(provider, decrypted)
    return decrypted


# ── Key Status Updates ────────────────────────────────────────────────────────

async def mark_key_cooldown(
    key_id: str,
    duration_seconds: int = 60,
    provider: Optional[str] = None
) -> None:
    """Mark a key as cooldown and invalidate its provider's cache entry."""
    cooldown_until = (
        datetime.datetime.now(datetime.timezone.utc) +
        datetime.timedelta(seconds=duration_seconds)
    ).isoformat()

    def _update():
        if not supabase:
            return
        try:
            supabase.rpc("increment_error_count", {"key_id": key_id}).execute()
            supabase.table("provider_keys")\
                .update({"status": "cooldown", "cooldown_until": cooldown_until})\
                .eq("id", key_id)\
                .execute()
            logger.info(
                f"[pool_manager] Key {key_id} → cooldown for {duration_seconds}s",
                extra={
                    "event": "key_cooldown", "key_id": key_id,
                    "duration_seconds": duration_seconds, "provider": provider,
                }
            )
        except Exception as e:
            logger.error(f"[pool_manager] Error marking key {key_id} cooldown: {e}", exc_info=True)

    await asyncio.to_thread(_update)
    get_key_cache().invalidate(provider)  # None → invalidate all (safe fallback)


async def mark_key_dead(
    key_id: str,
    error_message: str,
    provider: Optional[str] = None
) -> None:
    """Mark a key as dead (auth error) and invalidate its provider's cache entry."""
    def _update():
        if not supabase:
            return
        try:
            supabase.rpc("increment_error_count", {"key_id": key_id}).execute()
            supabase.table("provider_keys")\
                .update({"status": "dead", "cooldown_until": None})\
                .eq("id", key_id)\
                .execute()
            logger.warning(
                f"[pool_manager] Key {key_id} → DEAD",
                extra={"event": "key_dead", "key_id": key_id,
                       "error": error_message, "provider": provider}
            )
        except Exception as e:
            logger.error(f"[pool_manager] Error marking key {key_id} dead: {e}", exc_info=True)

    await asyncio.to_thread(_update)
    get_key_cache().invalidate(provider)


async def update_key_last_used(key_id: str) -> None:
    """Update LRU timestamp for a key. Fire-and-forget safe."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _update():
        if not supabase:
            return
        try:
            supabase.table("provider_keys")\
                .update({"last_used_at": now})\
                .eq("id", key_id)\
                .execute()
        except Exception as e:
            logger.debug(f"[pool_manager] update_key_last_used error for {key_id}: {e}")

    await asyncio.to_thread(_update)


# ── Request Logging ───────────────────────────────────────────────────────────

async def log_request(
    provider: str,
    model: str,
    key_id: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status_code: int,
    error_message: Optional[str] = None,
    request_id: Optional[str] = None,
    client_key_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Log a completed request to Supabase for metrics and billing."""
    def _insert():
        if not supabase:
            return
        try:
            supabase.table("request_logs").insert({
                "provider": provider,
                "model": model,
                "key_id": key_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "error_message": error_message,
                "request_id": request_id,
                "client_key_id": client_key_id,
                "metadata": metadata,
            }).execute()
        except Exception as e:
            logger.error(f"[pool_manager] Error logging request: {e}", exc_info=True)

    await asyncio.to_thread(_insert)


# ── Multi-Tenant Client Keys ──────────────────────────────────────────────────

def _hash_key(raw_key: str) -> str:
    """SHA-256 hash used to store client keys without exposing plaintext."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_client_key() -> str:
    """Generate a cryptographically-secure random client API key (ck_ prefix)."""
    return "ck_" + secrets.token_urlsafe(32)


async def get_client_key_info(raw_key: str) -> Optional[Dict[str, Any]]:
    """
    Look up a client key by its SHA-256 hash.
    Returns the client key record if active, else None.
    """
    key_hash = _hash_key(raw_key)

    def _query():
        if not supabase:
            return None
        try:
            result = supabase.table("client_keys")\
                .select("id, key_name, is_active, allowed_models, daily_token_limit")\
                .eq("key_hash", key_hash)\
                .eq("is_active", True)\
                .limit(1)\
                .execute()
            return (result.data or [None])[0]
        except Exception as e:
            logger.error(f"[pool_manager] Error fetching client key: {e}", exc_info=True)
            return None

    return await asyncio.to_thread(_query)


async def create_client_key(key_name: str, allowed_models: Optional[list] = None,
                            daily_token_limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Create a new client API key.  Returns the plaintext key (shown only once)
    and the DB record id.  Only the hash is persisted.
    """
    raw_key = generate_client_key()
    key_hash = _hash_key(raw_key)

    def _insert():
        if not supabase:
            raise RuntimeError("Supabase client not initialized. Check SUPABASE_URL and SUPABASE_KEY.")
        return supabase.table("client_keys").insert({
            "key_name": key_name,
            "key_hash": key_hash,
            "is_active": True,
            "allowed_models": allowed_models,
            "daily_token_limit": daily_token_limit,
        }).execute()

    result = await asyncio.to_thread(_insert)
    record = result.data[0] if result.data else {}
    return {"key": raw_key, "id": record.get("id"), "key_name": key_name}
