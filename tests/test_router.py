# tests/test_router.py
"""
Unit tests for src/core/router.py

Tests the fallback chain resolution logic:
- Static routing table lookup
- Heuristic name-based fallback
- DB-loaded routes override static ones
- Cache invalidation behavior
"""
import asyncio
import time
import pytest
from typing import Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

# ── Patch Supabase before importing anything that touches it ──────────────────
import sys
from unittest.mock import MagicMock

# Provide a dummy supabase module so pool_manager doesn't fail on import
sys.modules.setdefault("supabase", MagicMock())

from src.core.router import (
    _STATIC_ROUTING,
    _heuristic_fallback,
    resolve_fallback_chain,
    refresh_route_cache,
    get_route_cache,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_mock_supabase(routes: List[Dict]):
    """Return a Supabase-like mock that yields the given rows from model_routes."""
    mock_response = MagicMock()
    mock_response.data = routes

    mock_table = MagicMock()
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.order.return_value = mock_table
    mock_table.execute.return_value = mock_response

    mock_client = MagicMock()
    mock_client.table.return_value = mock_table
    return mock_client


# ── Tests: heuristic fallback ─────────────────────────────────────────────────

class TestHeuristicFallback:
    def test_gemini_prefix(self):
        chain = _heuristic_fallback("gemini-2.0-flash")
        assert chain[0] == ("gemini", "gemini-2.0-flash")
        assert any(p == "openrouter" for p, _ in chain)

    def test_claude_prefix(self):
        chain = _heuristic_fallback("claude-3-opus")
        assert chain[0][0] == "openrouter"
        assert "claude-3-opus" in chain[0][1]

    def test_llama_in_name(self):
        chain = _heuristic_fallback("llama3-70b")
        assert chain[0][0] == "groq"

    def test_gpt_prefix(self):
        chain = _heuristic_fallback("gpt-4o-custom")
        assert chain[0] == ("openai", "gpt-4o-custom")
        assert chain[1] == ("openrouter", "openai/gpt-4o-custom")

    def test_kimi_in_name(self):
        chain = _heuristic_fallback("kimi-k1.5")
        assert chain[0] == ("moonshot", "kimi-k1.5")
        assert chain[1] == ("openrouter", "moonshotai/kimi-k1.5")

    def test_deepseek_in_name(self):
        chain = _heuristic_fallback("deepseek-v3")
        assert chain[0] == ("dashscope", "deepseek-v3")
        assert chain[1] == ("openrouter", "deepseek/deepseek-v3")

    def test_qwen_in_name(self):
        chain = _heuristic_fallback("qwen-2.5-72b")
        assert chain[0] == ("dashscope", "qwen-2.5-72b")
        assert chain[1] == ("openrouter", "qwen/qwen-2.5-72b")

    def test_unknown_model_defaults_to_openrouter(self):
        chain = _heuristic_fallback("some-random-model-xyz")
        assert chain == [("openrouter", "some-random-model-xyz")]


# ── Tests: static routing table ───────────────────────────────────────────────

class TestStaticRouting:
    def test_deepseek_v4_1_flash_exists(self):
        assert "deepseek-v4.1-flash" in _STATIC_ROUTING
        assert _STATIC_ROUTING["deepseek-v4.1-flash"][0] == ("dashscope", "deepseek-v4.1-flash")

    def test_qwen_cloud_models_exist(self):
        qwen_models = [
            "qwen3.8-max", "qwen3.7-max", "qwen3.7-plus", "qwen3.7-flash",
            "qwen3.6-plus", "qwen3.6-flash", "qwen3.5-plus", "qwen3.5-flash",
            "qwen3-coder-plus", "qwen3-coder-flash", "qwen3-max",
            "qwen3-next-80b-a3b-thinking", "qwen3-next-80b-a3b-instruct",
            "qwen3-32b", "qwen3-30b-a3b"
        ]
        for model in qwen_models:
            assert model in _STATIC_ROUTING, f"Model {model} should be in _STATIC_ROUTING"
            assert _STATIC_ROUTING[model][0] == ("dashscope", model)

    def test_combo_smart_exists(self):
        assert "combo-smart" in _STATIC_ROUTING

    def test_combo_fast_exists(self):
        assert "combo-fast" in _STATIC_ROUTING

    def test_new_combo_models_exist(self):
        new_combos = ["combo-anti-limit", "combo-chatbot-cheap", "combo-chatbot-hemat", "combo-coding-antilimit", "combo-coding"]
        for combo in new_combos:
            assert combo in _STATIC_ROUTING
            assert len(_STATIC_ROUTING[combo]) >= 4

    def test_combo_smart_has_multiple_fallbacks(self):
        chain = _STATIC_ROUTING["combo-smart"]
        assert len(chain) >= 2

    def test_all_chains_are_non_empty_tuples(self):
        for model, chain in _STATIC_ROUTING.items():
            assert isinstance(chain, list), f"{model} chain should be a list"
            for entry in chain:
                assert len(entry) == 2, f"{model} entries should be (provider, model) tuples"
                provider, target = entry
                assert isinstance(provider, str) and provider, f"{model} provider should be non-empty string"
                assert isinstance(target, str) and target, f"{model} target should be non-empty string"


# ── Tests: resolve_fallback_chain (async) ─────────────────────────────────────

class TestResolveFallbackChain:
    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Reset the in-memory route cache before each test."""
        import src.core.router as router_module
        router_module._route_cache = {}
        router_module._cache_loaded_at = 0.0
        yield

    def test_static_model_resolved(self):
        """Known static models should resolve without hitting DB."""
        mock_supabase = make_mock_supabase([])  # Empty DB → uses static fallback
        chain = asyncio.get_event_loop().run_until_complete(
            resolve_fallback_chain("combo-fast", mock_supabase)
        )
        assert len(chain) >= 1
        assert chain[0][0] in ("groq", "gemini", "openrouter")

    def test_db_routes_override_static(self):
        """Routes from DB should take precedence over the static table."""
        import time as _time
        import src.core.router as router_module

        # Directly set the cache as if refresh_route_cache already ran with DB data
        # This tests that resolve_fallback_chain correctly reads from the cache
        router_module._route_cache = {
            **router_module._STATIC_ROUTING,
            "combo-smart": [("custom-provider", "custom-model-v1")],  # DB override
        }
        # Mark cache as fresh so it's not refreshed
        router_module._cache_loaded_at = _time.monotonic()

        chain = asyncio.get_event_loop().run_until_complete(
            resolve_fallback_chain("combo-smart", MagicMock())
        )
        assert chain[0] == ("custom-provider", "custom-model-v1")

    def test_unknown_model_uses_heuristic(self):
        """Models not in static or DB routing use the heuristic fallback."""
        mock_supabase = make_mock_supabase([])
        chain = asyncio.get_event_loop().run_until_complete(
            resolve_fallback_chain("gemini-99-ultra", mock_supabase)
        )
        assert chain[0][0] == "gemini"

    def test_cache_used_on_second_call(self):
        """Second call within TTL should NOT query Supabase again."""
        db_rows = [
            {"virtual_model": "my-model", "provider": "groq", "target_model": "llama3-8b-8192", "priority": 1},
        ]
        mock_supabase = make_mock_supabase(db_rows)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(resolve_fallback_chain("my-model", mock_supabase))

        # Reset the mock call count
        mock_supabase.table.reset_mock()

        # Second call — should use cache, not hit table()
        loop.run_until_complete(resolve_fallback_chain("my-model", mock_supabase))
        mock_supabase.table.assert_not_called()
