# src/core/router.py
import asyncio
import time
from typing import List, Tuple, Dict, Optional
from supabase import Client

from src.core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Semantic capability metadata
# ---------------------------------------------------------------------------
# Tells the router which models support multimodal input (image_url).
# Models NOT listed here are assumed text-only — they will be deprioritised
# when the request contains image content (see filter_chain_for_multimodal).
MODEL_CAPABILITIES: Dict[str, Dict] = {
    # Google Gemini — all multimodal
    "gemini-2.5-pro":    {"multimodal": True, "context_window": 1_000_000},
    "gemini-2.0-flash":  {"multimodal": True, "context_window": 1_000_000},
    "gemini-3.5-flash":  {"multimodal": True, "context_window": 1_000_000},
    # OpenAI vision models
    "gpt-4o":            {"multimodal": True, "context_window": 128_000},
    "gpt-4o-mini":       {"multimodal": True, "context_window": 128_000},
    "gpt-4-turbo":       {"multimodal": True, "context_window": 128_000},
    # Anthropic — all Claude 3+ models support vision
    "claude-sonnet-4":   {"multimodal": True, "context_window": 200_000},
    "claude-3-5-sonnet": {"multimodal": True, "context_window": 200_000},
    # DeepSeek — vision model
    "deepseek-v4.1-flash": {"multimodal": True, "context_window": 128_000},
    # Qwen models with vision support
    "qwen3.8-max":        {"multimodal": True, "context_window": 1_000_000},
    "qwen3.7-max":        {"multimodal": True, "context_window": 1_000_000},
    "qwen3.7-plus":       {"multimodal": True, "context_window": 1_000_000},
}


def filter_chain_for_multimodal(
    chain: List[Tuple[str, str]]
) -> List[Tuple[str, str]]:
    """
    For multimodal requests (messages with image_url), prefer models that are
    known to support images.  Falls back to the original full chain if no
    multimodal-capable provider is available (graceful degradation).
    """
    capable = [
        (prov, mdl) for prov, mdl in chain
        if MODEL_CAPABILITIES.get(mdl, {}).get("multimodal", False)
    ]
    return capable if capable else chain  # Graceful degradation


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
    "combo-anti-limit": [
        ("dashscope", "deepseek-v4.1-flash"),
        ("gemini", "gemini-2.0-flash"),
        ("groq", "llama-3.3-70b-versatile"),
        ("dashscope", "qwen3.7-flash"),
        ("openrouter", "google/gemini-2.0-flash-001"),
    ],
    "combo-chatbot-cheap": [
        ("dashscope", "qwen3.5-flash"),
        ("gemini", "gemini-2.0-flash"),
        ("groq", "llama-3.1-8b-instant"),
        ("dashscope", "qwen3.6-flash"),
        ("openrouter", "meta-llama/llama-3.1-8b-instruct"),
    ],
    "combo-chatbot-hemat": [
        ("dashscope", "qwen3.5-flash"),
        ("gemini", "gemini-2.0-flash"),
        ("groq", "llama-3.1-8b-instant"),
        ("dashscope", "qwen3.6-flash"),
        ("openrouter", "meta-llama/llama-3.1-8b-instruct"),
    ],
    "combo-coding-antilimit": [
        ("dashscope", "qwen3-coder-plus"),
        ("dashscope", "qwen3-coder-flash"),
        ("dashscope", "deepseek-v4.1-flash"),
        ("openrouter", "anthropic/claude-3.5-sonnet"),
        ("groq", "llama-3.3-70b-versatile"),
        ("openrouter", "qwen/qwen3-coder-plus"),
    ],
    "combo-coding": [
        ("dashscope", "qwen3-coder-plus"),
        ("dashscope", "qwen3-coder-flash"),
        ("dashscope", "deepseek-v4.1-flash"),
        ("openrouter", "anthropic/claude-3.5-sonnet"),
        ("groq", "llama-3.3-70b-versatile"),
        ("openrouter", "qwen/qwen3-coder-plus"),
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
    # OpenAI models
    "gpt-4o": [
        ("openai", "gpt-4o"),
        ("openrouter", "openai/gpt-4o"),
    ],
    "gpt-4o-mini": [
        ("openai", "gpt-4o-mini"),
        ("openrouter", "openai/gpt-4o-mini"),
    ],
    "gpt-4-turbo": [
        ("openai", "gpt-4-turbo"),
        ("openrouter", "openai/gpt-4-turbo"),
    ],
    "gpt-3.5-turbo": [
        ("openai", "gpt-3.5-turbo"),
        ("openrouter", "openai/gpt-3.5-turbo"),
    ],
    "o1": [
        ("openai", "o1"),
        ("openrouter", "openai/o1"),
    ],
    "o3-mini": [
        ("openai", "o3-mini"),
        ("openrouter", "openai/o3-mini"),
    ],
    # Kimi / Moonshot models
    "kimi-latest": [
        ("moonshot", "kimi-latest"),
        ("openrouter", "moonshotai/kimi-latest"),
    ],
    "kimi-k1.5": [
        ("moonshot", "kimi-k1.5"),
        ("openrouter", "moonshotai/kimi-k1.5"),
    ],
    "moonshot-v1-8k": [
        ("moonshot", "moonshot-v1-8k"),
        ("openrouter", "moonshotai/moonshot-v1-8k"),
    ],
    "moonshot-v1-32k": [
        ("moonshot", "moonshot-v1-32k"),
        ("openrouter", "moonshotai/moonshot-v1-32k"),
    ],
    "moonshot-v1-128k": [
        ("moonshot", "moonshot-v1-128k"),
        ("openrouter", "moonshotai/moonshot-v1-128k"),
    ],
    # DeepSeek models
    "deepseek-v4.1-flash": [
        ("dashscope", "deepseek-v4.1-flash"),
        ("openrouter", "deepseek/deepseek-chat"),
    ],
    "deepseek-r1": [
        ("dashscope", "deepseek-r1"),
        ("openrouter", "deepseek/deepseek-r1"),
    ],
    "deepseek-v3": [
        ("dashscope", "deepseek-v3"),
        ("openrouter", "deepseek/deepseek-chat"),
    ],
    # Qwen models (DashScope / QwenCloud)
    "qwen3.8-max": [
        ("dashscope", "qwen3.8-max"),
        ("openrouter", "qwen/qwen3.8-max"),
    ],
    "qwen3.7-max": [
        ("dashscope", "qwen3.7-max"),
        ("openrouter", "qwen/qwen3.7-max"),
    ],
    "qwen3.7-plus": [
        ("dashscope", "qwen3.7-plus"),
        ("openrouter", "qwen/qwen3.7-plus"),
    ],
    "qwen3.7-flash": [
        ("dashscope", "qwen3.7-flash"),
        ("openrouter", "qwen/qwen3.7-flash"),
    ],
    "qwen3.6-plus": [
        ("dashscope", "qwen3.6-plus"),
        ("openrouter", "qwen/qwen3.6-plus"),
    ],
    "qwen3.6-flash": [
        ("dashscope", "qwen3.6-flash"),
        ("openrouter", "qwen/qwen3.6-flash"),
    ],
    "qwen3.5-plus": [
        ("dashscope", "qwen3.5-plus"),
        ("openrouter", "qwen/qwen3.5-plus"),
    ],
    "qwen3.5-flash": [
        ("dashscope", "qwen3.5-flash"),
        ("openrouter", "qwen/qwen3.5-flash"),
    ],
    "qwen3-coder-plus": [
        ("dashscope", "qwen3-coder-plus"),
        ("openrouter", "qwen/qwen3-coder-plus"),
    ],
    "qwen3-coder-flash": [
        ("dashscope", "qwen3-coder-flash"),
        ("openrouter", "qwen/qwen3-coder-flash"),
    ],
    "qwen3-max": [
        ("dashscope", "qwen3-max"),
        ("openrouter", "qwen/qwen3-max"),
    ],
    "qwen3-next-80b-a3b-thinking": [
        ("dashscope", "qwen3-next-80b-a3b-thinking"),
        ("openrouter", "qwen/qwen3-next-80b-a3b-thinking"),
    ],
    "qwen3-next-80b-a3b-instruct": [
        ("dashscope", "qwen3-next-80b-a3b-instruct"),
        ("openrouter", "qwen/qwen3-next-80b-a3b-instruct"),
    ],
    "qwen3-32b": [
        ("dashscope", "qwen3-32b"),
        ("openrouter", "qwen/qwen3-32b"),
    ],
    "qwen3-30b-a3b": [
        ("dashscope", "qwen3-30b-a3b"),
        ("openrouter", "qwen/qwen3-30b-a3b"),
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
        logger.error(f"[router] Failed to load routes from DB: {e}. Using static fallback.",
                     extra={"event": "route_db_error"}, exc_info=True)
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
        logger.info(
            f"[router] Route cache refreshed — {len(_route_cache)} entries "
            f"({len(db_routes)} from DB, {len(_STATIC_ROUTING)} static defaults)",
            extra={
                "event": "route_cache_refreshed",
                "total": len(_route_cache),
                "from_db": len(db_routes),
                "static": len(_STATIC_ROUTING),
            }
        )


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
    elif "llama" in model_lower:
        return [("groq", requested_model), ("openrouter", requested_model)]
    elif "gpt-" in model_lower or model_lower.startswith("o1") or model_lower.startswith("o3"):
        return [("openai", requested_model), ("openrouter", f"openai/{requested_model}")]
    elif "kimi" in model_lower or "moonshot" in model_lower:
        return [("moonshot", requested_model), ("openrouter", f"moonshotai/{requested_model}")]
    elif "deepseek" in model_lower:
        openrouter_target = requested_model if requested_model.startswith("deepseek/") else f"deepseek/{requested_model}"
        return [("dashscope", requested_model), ("openrouter", openrouter_target)]
    elif "qwen" in model_lower:
        openrouter_target = requested_model if requested_model.startswith("qwen/") else f"qwen/{requested_model}"
        return [("dashscope", requested_model), ("openrouter", openrouter_target)]
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
