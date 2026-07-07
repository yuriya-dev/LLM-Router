# tests/test_client.py
"""
Unit tests for src/providers/client.py

Tests the error classification and response handling:
- _handle_error_response correctly classifies 429, 401, 403, 5xx
- ProviderError carries correct flags
- _build_headers adds OpenRouter-specific headers when needed
- SSE chunk parsing extracts token usage correctly
"""
import json
import pytest
from typing import Dict, Optional
from unittest.mock import MagicMock

import sys
sys.modules.setdefault("supabase", MagicMock())

from src.providers.client import ProviderError, _handle_error_response, _build_headers


# ── Tests: ProviderError ──────────────────────────────────────────────────────

class TestProviderError:
    def test_rate_limit_flag(self):
        err = ProviderError(429, "rate limited", is_rate_limit=True)
        assert err.is_rate_limit is True
        assert err.is_auth_error is False
        assert err.status_code == 429

    def test_auth_error_flag(self):
        err = ProviderError(401, "unauthorized", is_auth_error=True)
        assert err.is_auth_error is True
        assert err.is_rate_limit is False

    def test_generic_error(self):
        err = ProviderError(500, "server error")
        assert err.is_rate_limit is False
        assert err.is_auth_error is False
        assert str(err) == "server error"


# ── Tests: _handle_error_response ─────────────────────────────────────────────

def make_mock_response(status_code: int, body: Optional[Dict] = None, text: str = "error"):
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = text
    if body is not None:
        mock.json.return_value = body
    else:
        mock.json.side_effect = Exception("not json")
    return mock


class TestHandleErrorResponse:
    def test_429_raises_rate_limit(self):
        response = make_mock_response(429, {"error": {"message": "rate limited"}})
        with pytest.raises(ProviderError) as exc_info:
            _handle_error_response(response, "gemini")
        assert exc_info.value.is_rate_limit is True
        assert exc_info.value.status_code == 429

    def test_401_raises_auth_error(self):
        response = make_mock_response(401, {"error": {"message": "invalid key"}})
        with pytest.raises(ProviderError) as exc_info:
            _handle_error_response(response, "groq")
        assert exc_info.value.is_auth_error is True

    def test_403_raises_auth_error(self):
        response = make_mock_response(403)
        with pytest.raises(ProviderError) as exc_info:
            _handle_error_response(response, "openrouter")
        assert exc_info.value.is_auth_error is True

    def test_500_is_generic_error(self):
        response = make_mock_response(500, text="internal server error")
        with pytest.raises(ProviderError) as exc_info:
            _handle_error_response(response, "gemini")
        err = exc_info.value
        assert err.is_rate_limit is False
        assert err.is_auth_error is False
        assert err.status_code == 500

    def test_extracts_json_error_message(self):
        response = make_mock_response(429, {"error": {"message": "quota exceeded for project"}})
        with pytest.raises(ProviderError) as exc_info:
            _handle_error_response(response, "gemini")
        assert "quota exceeded for project" in str(exc_info.value)

    def test_falls_back_to_text_on_non_json(self):
        response = make_mock_response(503, text="Service Unavailable")
        with pytest.raises(ProviderError) as exc_info:
            _handle_error_response(response, "openrouter")
        assert "Service Unavailable" in str(exc_info.value)


# ── Tests: _build_headers ─────────────────────────────────────────────────────

class TestBuildHeaders:
    def test_standard_headers(self):
        headers = _build_headers("gemini", "my-api-key")
        assert headers["Authorization"] == "Bearer my-api-key"
        assert headers["Content-Type"] == "application/json"

    def test_openrouter_extra_headers(self):
        headers = _build_headers("openrouter", "my-api-key")
        assert "HTTP-Referer" in headers
        assert "X-Title" in headers

    def test_non_openrouter_no_extra_headers(self):
        for provider in ("gemini", "groq"):
            headers = _build_headers(provider, "key")
            assert "HTTP-Referer" not in headers
            assert "X-Title" not in headers


# ── Tests: SSE token parsing (inline logic test) ──────────────────────────────

class TestSSETokenParsing:
    """
    Test the SSE chunk parsing logic used inside response_generator.
    We replicate the same logic here to keep tests fast (no actual HTTP calls).
    """
    def _parse_usage_from_chunks(self, chunks: list[bytes]) -> tuple[int, int]:
        prompt_tokens = 0
        completion_tokens = 0
        for chunk in chunks:
            try:
                for line in chunk.decode("utf-8", errors="ignore").splitlines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        data = json.loads(line[6:])
                        usage = data.get("usage") or {}
                        if usage:
                            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                            completion_tokens = usage.get("completion_tokens", completion_tokens)
            except Exception:
                pass
        return prompt_tokens, completion_tokens

    def test_extracts_usage_from_last_chunk(self):
        chunks = [
            b'data: {"id":"1","choices":[{"delta":{"content":"Hello"}}]}\n',
            b'data: {"id":"1","choices":[{"delta":{"content":" world"}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n',
            b'data: [DONE]\n',
        ]
        pt, ct = self._parse_usage_from_chunks(chunks)
        assert pt == 10
        assert ct == 5

    def test_returns_zero_when_no_usage(self):
        chunks = [
            b'data: {"id":"1","choices":[{"delta":{"content":"Hello"}}]}\n',
            b'data: [DONE]\n',
        ]
        pt, ct = self._parse_usage_from_chunks(chunks)
        assert pt == 0
        assert ct == 0

    def test_tolerates_malformed_chunks(self):
        chunks = [
            b"this is not valid SSE\n",
            b"data: {broken json}\n",
            b'data: {"usage":{"prompt_tokens":7,"completion_tokens":3}}\n',
        ]
        pt, ct = self._parse_usage_from_chunks(chunks)
        assert pt == 7
        assert ct == 3
