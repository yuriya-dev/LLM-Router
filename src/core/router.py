# src/core/router.py
import asyncio
import time
from typing import List, Tuple, Dict, Optional
from supabase import Client

# ---------------------------------------------------------------------------
# Static fallback (used when DB is unreachable or model_routes table is empty)
# This is the hardcoded routing kept as a safe default / bootstrap.
# ---------------------------------------------------------------------------
_STATIC_ROUTING: Dict[str, List[Tuple[str, str]]] = {
    # Virtual Models
    "combo-smart": [
        ("openrouter", "anthropic/claude-sonnet-4"),
        ("gemini", "gemini-2.5-pro"),
    ],
    "combo-fast": [
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.0-flash"),
    ],
    # Direct mappings with fallback alternatives
    "gemini-1.5-pro": [
        ("gemini", "gemini-2.5-pro"),
        ("openrouter", "google/gemini-2.5-pro"),
    ],
    "gemini-1.5-flash": [
        ("gemini", "gemini-2.0-flash"),
        ("openrouter", "google/gemini-3.5-flash"),
    ],
    "claude-3-5-sonnet": [
        ("openrouter", "anthropic/claude-sonnet-4"),
        ("gemini", "gemini-2.5-pro"),
    ],
    "llama3-8b": [
        ("groq", "llama-3.1-8b-instant"),
        ("openrouter", "meta-llama/llama-3-8b-instruct"),
    ],
}

# ---------------------------------------------------------------------------
# In-memory cache for routes loaded from Supabase
# TTL: 60 seconds — routes refresh automatically without a deploy
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 60
_route_cache: Dict[str, List[Tuple[str, str]]] = {}
_cache_loaded_at: float = 0.0
_cache_lock = asyncio.Lock()


async def _load_routes_from_db(supabase: Client) -> Dict[str, List[Tuple[str, str]]]:
    """
    Load all routes from the Supabase `model_routes` table.
    Returns a dict of {virtual_model: [(provider, target_model), ...]} sorted by priority.
    """
    def _query():
        return supabase.table("model_routes")\
            .select("virtual_model, provider, target_model, priority")\
            .eq("enabled", True)\
            .order("virtual_model")\
            .order("priority", desc=False)\
            .execute()

    try:
        response = await asyncio.to_thread(_query)
        routes: Dict[str, List[Tuple[str, str]]] = {}
        for row in (response.data or []):
            key = row["virtual_model"]
            routes.setdefault(key, []).append((row["provider"], row["target_model"]))
        return routes
    except Exception as e:
        print(f"[router] Failed to load routes from DB: {e}. Using static fallback.")
        return {}


async def refresh_route_cache(supabase: Client):
    """
    Reload the route cache from Supabase. Thread-safe via asyncio.Lock.
    Called automatically when cache TTL expires.
    """
    global _route_cache, _cache_loaded_at
    async with _cache_lock:
        db_routes = await _load_routes_from_db(supabase)
        # Merge: DB routes override static ones, static ones fill the gaps
        _route_cache = {**_STATIC_ROUTING, **db_routes}
        _cache_loaded_at = time.monotonic()
        print(f"[router] Route cache refreshed — {len(_route_cache)} entries "
              f"({len(db_routes)} from DB, rest from static defaults).")


async def get_route_cache(supabase: Client) -> Dict[str, List[Tuple[str, str]]]:
    """
    Return the route cache, refreshing it from DB if the TTL has expired or if the cache is empty.
    """
    if not _route_cache or (time.monotonic() - _cache_loaded_at > _CACHE_TTL_SECONDS):
        await refresh_route_cache(supabase)
    return _route_cache


def _heuristic_fallback(requested_model: str) -> List[Tuple[str, str]]:
    """
    Determine a fallback chain from the model name prefix when no explicit
    route exists in either the DB or the static config.
    """
    model_lower = requested_model.lower()

    # Clean up common naming variations for Claude 3.5 Sonnet
    if "claude-3-5-sonnet" in model_lower:
        return [("openrouter", "anthropic/claude-3.5-sonnet")]

    # Clean up common naming variations for Mistral Large
    if "mistral-large" in model_lower:
        return [("mistral", "mistral-large-latest"), ("openrouter", "mistralai/mistral-large")]

    if requested_model.startswith("gemini-"):
        # Map deprecated/invalid gemini-2.5-flash and gemini-1.5-flash to 3.5-flash
        target = requested_model
        if "2.5-flash" in requested_model or "1.5-flash" in requested_model:
            target = "gemini-3.5-flash"
        return [("gemini", target), ("openrouter", f"google/{target}")]
    elif requested_model.startswith("claude-"):
        return [("openrouter", f"anthropic/{requested_model}")]
    elif "llama" in requested_model.lower():
        return [("groq", requested_model), ("openrouter", requested_model)]
    # Default: OpenRouter supports almost everything
    return [("openrouter", requested_model)]


async def resolve_fallback_chain(
    requested_model: str,
    supabase: Client,
) -> List[Tuple[str, str]]:
    """
    Given a model name requested by the client, resolve the ordered fallback chain
    of (provider, destination_model_name) to attempt.

    Priority:
      1. Explicit route from DB/cache (refreshed every 60 s — no deploy needed)
      2. Static hardcoded routes (kept as safe defaults)
      3. Heuristic name-based fallback
    """
    routes = await get_route_cache(supabase)

    if requested_model in routes:
        return routes[requested_model]

    return _heuristic_fallback(requested_model)
