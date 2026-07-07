# src/core/pool_manager.py
import asyncio
import datetime
from typing import List, Dict, Any, Optional
from supabase import create_client, Client
from src.config import settings

# Shared Supabase client — also exported so router.py can reuse it
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


async def restore_all_expired_cooldowns():
    """
    Restore all keys whose cooldown period has expired to 'healthy'.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    def _restore():
        try:
            supabase.table("provider_keys")\
                .update({
                    "status": "healthy",
                    "cooldown_until": None,
                    "error_count": 0
                })\
                .eq("status", "cooldown")\
                .lt("cooldown_until", now)\
                .execute()
        except Exception as e:
            print(f"Error restoring expired cooldown keys: {e}")
    await asyncio.to_thread(_restore)


async def get_healthy_keys(provider: str) -> List[Dict[str, Any]]:
    """
    Fetch healthy keys for a specific provider.
    Also restores keys whose cooldown period has expired.
    Runs in a thread pool to avoid blocking the async event loop.
    """
    await restore_all_expired_cooldowns()

    def _fetch_healthy():
        try:
            response = supabase.table("provider_keys")\
                .select("id, provider, key_name, key_value, priority")\
                .eq("provider", provider)\
                .eq("status", "healthy")\
                .order("priority", desc=False)\
                .order("last_used_at", nullsfirst=True)\
                .execute()
            return response.data or []
        except Exception as e:
            print(f"Error fetching healthy keys from Supabase: {e}")
            return []

    # Fetch healthy keys (non-blocking)
    return await asyncio.to_thread(_fetch_healthy)


async def mark_key_cooldown(key_id: str, duration_seconds: int = 60):
    """
    Mark a key as in cooldown state.
    Runs in a thread pool to avoid blocking the async event loop.
    """
    cooldown_until = (
        datetime.datetime.now(datetime.timezone.utc) +
        datetime.timedelta(seconds=duration_seconds)
    ).isoformat()

    def _update():
        try:
            # Use Supabase RPC to atomically increment error_count
            supabase.rpc("increment_error_count", {"key_id": key_id}).execute()
            supabase.table("provider_keys")\
                .update({
                    "status": "cooldown",
                    "cooldown_until": cooldown_until,
                })\
                .eq("id", key_id)\
                .execute()
            print(f"Key {key_id} put in cooldown until {cooldown_until}")
        except Exception as e:
            print(f"Error marking key as cooldown: {e}")

    await asyncio.to_thread(_update)


async def mark_key_dead(key_id: str, error_message: str):
    """
    Mark a key as dead (invalid/auth error).
    Runs in a thread pool to avoid blocking the async event loop.
    """
    def _update():
        try:
            # Increment error_count and mark dead
            supabase.rpc("increment_error_count", {"key_id": key_id}).execute()
            supabase.table("provider_keys")\
                .update({
                    "status": "dead",
                    "cooldown_until": None
                })\
                .eq("id", key_id)\
                .execute()
            print(f"Key {key_id} marked as DEAD due to: {error_message}")
        except Exception as e:
            print(f"Error marking key as dead: {e}")

    await asyncio.to_thread(_update)


async def update_key_last_used(key_id: str):
    """
    Update last used timestamp for a key to maintain LRU/Round-robin ordering.
    Runs in a thread pool to avoid blocking the async event loop.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _update():
        try:
            supabase.table("provider_keys")\
                .update({"last_used_at": now})\
                .eq("id", key_id)\
                .execute()
        except Exception as e:
            print(f"Error updating key last used: {e}")

    await asyncio.to_thread(_update)


async def log_request(
    provider: str,
    model: str,
    key_id: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status_code: int,
    error_message: Optional[str] = None
):
    """
    Log request history to Supabase for metrics and audits.
    Runs in a thread pool to avoid blocking the async event loop.
    """
    def _insert():
        try:
            supabase.table("request_logs").insert({
                "provider": provider,
                "model": model,
                "key_id": key_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "error_message": error_message
            }).execute()
        except Exception as e:
            print(f"Error logging request: {e}")

    await asyncio.to_thread(_insert)
