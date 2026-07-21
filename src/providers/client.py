# src/providers/client.py
import json
import httpx
from contextlib import asynccontextmanager
from typing import Dict, Any, AsyncGenerator, Callable, Optional, Awaitable

PROVIDER_ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "kilo": "https://api.kilo.ai/api/gateway/chat/completions",
    "cerebras": "https://api.cerebras.ai/v1/chat/completions",
    "cerebas": "https://api.cerebras.ai/v1/chat/completions",  # support potential typo/alternative spelling
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "moonshot": "https://api.moonshot.ai/v1/chat/completions",
    "kimi": "https://api.moonshot.ai/v1/chat/completions",
}

# Per-provider timeouts (seconds) to enable faster fallback when a provider hangs
PROVIDER_TIMEOUTS = {
    "groq": httpx.Timeout(15.0),
    "gemini": httpx.Timeout(45.0),
    "openrouter": httpx.Timeout(90.0),
    "kilo": httpx.Timeout(30.0),
    "cerebras": httpx.Timeout(30.0),
    "cerebas": httpx.Timeout(30.0),
    "mistral": httpx.Timeout(30.0),
    "openai": httpx.Timeout(45.0),
    "moonshot": httpx.Timeout(45.0),
    "kimi": httpx.Timeout(45.0),
}

# Shared HTTP client — initialized once at app startup via init_http_client()
# Connection pooling is reused across all requests (no repeated TCP/TLS handshake overhead)
_http_client: Optional[httpx.AsyncClient] = None


def init_http_client():
    """Initialize the shared HTTP client. Call this at app startup."""
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),   # default fallback timeout
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        http2=True,
    )


async def close_http_client():
    """Gracefully close the shared HTTP client. Call this at app shutdown."""
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


def _get_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized. Call init_http_client() at startup.")
    return _http_client


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str, is_rate_limit: bool = False, is_auth_error: bool = False, is_request_error: bool = False):
        self.status_code = status_code
        self.message = message
        self.is_rate_limit = is_rate_limit
        self.is_auth_error = is_auth_error
        self.is_request_error = is_request_error
        super().__init__(message)


def _build_headers(provider: str, api_key: str) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://9router-custom.local"
        headers["X-Title"] = "Custom LLM Router"
    return headers


async def execute_request(
    provider: str,
    target_model: str,
    api_key: str,
    request_body: Dict[str, Any]
) -> httpx.Response:
    """
    Sends a non-streaming request to the provider using the shared HTTP client.
    """
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        raise ProviderError(500, f"Unknown provider: {provider}")

    headers = _build_headers(provider, api_key)
    body = dict(request_body)
    body["model"] = target_model

    client = _get_client()
    timeout = PROVIDER_TIMEOUTS.get(provider)

    try:
        response = await client.post(endpoint, json=body, headers=headers, timeout=timeout)
    except httpx.RequestError as exc:
        raise ProviderError(500, f"Network error connecting to {provider}: {str(exc)}")

    if response.status_code != 200:
        _handle_error_response(response, provider)

    return response


async def execute_stream_request(
    provider: str,
    target_model: str,
    api_key: str,
    request_body: Dict[str, Any],
    on_complete: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> AsyncGenerator[bytes, None]:
    """
    Sends a streaming request and yields SSE chunks using the shared HTTP client.
    Parses each chunk for token usage and calls on_complete(prompt_tokens, completion_tokens)
    after the stream ends so the caller can log accurate metrics without blocking.
    """
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        raise ProviderError(500, f"Unknown provider: {provider}")

    headers = _build_headers(provider, api_key)
    body = dict(request_body)
    body["model"] = target_model
    body["stream"] = True
    # Request usage stats in the final chunk (supported by OpenAI-compatible APIs)
    body.setdefault("stream_options", {"include_usage": True})

    client = _get_client()
    timeout = PROVIDER_TIMEOUTS.get(provider)

    try:
        request = client.build_request("POST", endpoint, json=body, headers=headers, timeout=timeout)
        response = await client.send(request, stream=True)
    except httpx.RequestError as exc:
        raise ProviderError(500, f"Network error connecting to {provider}: {str(exc)}")

    if response.status_code != 200:
        await response.aread()
        _handle_error_response(response, provider)

    async def response_generator() -> AsyncGenerator[bytes, None]:
        prompt_tokens = 0
        completion_tokens = 0
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
                # Parse SSE lines to extract usage from any chunk that has it
                try:
                    for line in chunk.decode("utf-8", errors="ignore").splitlines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            data = json.loads(line[6:])
                            usage = data.get("usage") or {}
                            if usage:
                                prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                                completion_tokens = usage.get("completion_tokens", completion_tokens)
                except Exception:
                    pass  # Non-fatal: skip malformed chunks
        finally:
            await response.aclose()
            # Notify caller with final token counts (fires after stream ends)
            if on_complete:
                await on_complete(prompt_tokens, completion_tokens)

    return response_generator()


def _handle_error_response(response: httpx.Response, provider: str):
    status_code = response.status_code
    error_text = response.text

    is_rate_limit = status_code == 429
    is_auth_error = status_code in (401, 403, 402)
    is_request_error = status_code in (400, 404, 413, 422)

    # Try to parse json error if available
    try:
        err_json = response.json()
        if "error" in err_json:
            error_text = err_json["error"].get("message", error_text)
    except Exception:
        pass

    raise ProviderError(
        status_code=status_code,
        message=f"{provider} returned error {status_code}: {error_text}",
        is_rate_limit=is_rate_limit,
        is_auth_error=is_auth_error,
        is_request_error=is_request_error
    )
