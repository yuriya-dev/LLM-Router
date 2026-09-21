# src/providers/client.py
"""
Provider HTTP client with:
  - Per-provider base URLs and timeouts
  - Retry + exponential backoff for transient network errors
  - Per-request timeout override (from X-Request-Timeout header)
  - SSE streaming with end-of-stream token usage extraction
  - Structured error classification (rate-limit / auth / request / server)
"""
import asyncio
import json
import logging
from typing import AsyncGenerator, Callable, Dict, Any, Optional, Awaitable

import httpx

from src.core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDER_ENDPOINTS: Dict[str, str] = {
    "gemini":    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "groq":      "https://api.groq.com/openai/v1/chat/completions",
    "openrouter":"https://openrouter.ai/api/v1/chat/completions",
    "kilo":      "https://api.kilo.ai/api/gateway/chat/completions",
    "cerebras":  "https://api.cerebras.ai/v1/chat/completions",
    "cerebas":   "https://api.cerebras.ai/v1/chat/completions",   # typo alias
    "mistral":   "https://api.mistral.ai/v1/chat/completions",
    "openai":    "https://api.openai.com/v1/chat/completions",
    "moonshot":  "https://api.moonshot.ai/v1/chat/completions",
    "kimi":      "https://api.moonshot.ai/v1/chat/completions",
    "dashscope": "https://maas.qwencloudapi.com/compatible-mode/v1/chat/completions",
    "qwen":      "https://maas.qwencloudapi.com/compatible-mode/v1/chat/completions",
    "qwencloud": "https://maas.qwencloudapi.com/compatible-mode/v1/chat/completions",
}

# Default per-provider timeouts (seconds) — lower = faster fallback on hang
PROVIDER_TIMEOUTS: Dict[str, httpx.Timeout] = {
    "groq":       httpx.Timeout(15.0),
    "gemini":     httpx.Timeout(45.0),
    "openrouter": httpx.Timeout(90.0),
    "kilo":       httpx.Timeout(30.0),
    "cerebras":   httpx.Timeout(30.0),
    "cerebas":    httpx.Timeout(30.0),
    "mistral":    httpx.Timeout(30.0),
    "openai":     httpx.Timeout(45.0),
    "moonshot":   httpx.Timeout(45.0),
    "kimi":       httpx.Timeout(45.0),
    "dashscope":  httpx.Timeout(60.0),
    "qwen":       httpx.Timeout(60.0),
    "qwencloud":  httpx.Timeout(60.0),
}

# ---------------------------------------------------------------------------
# Shared HTTP client (connection-pooled, HTTP/2)
# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None


def init_http_client() -> None:
    """Initialise the shared async HTTP client at app startup."""
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
        http2=True,
    )
    logger.info("[client] Shared HTTP client initialised",
                extra={"event": "http_client_init"})


async def close_http_client() -> None:
    """Gracefully close the shared async HTTP client at app shutdown."""
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


def _get_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialised. Call init_http_client() at startup.")
    return _http_client


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        is_rate_limit: bool = False,
        is_auth_error: bool = False,
        is_request_error: bool = False,
    ):
        self.status_code = status_code
        self.message = message
        self.is_rate_limit = is_rate_limit
        self.is_auth_error = is_auth_error
        self.is_request_error = is_request_error
        super().__init__(message)


def _handle_error_response(response: httpx.Response, provider: str) -> None:
    status_code = response.status_code
    error_text = response.text

    is_rate_limit    = status_code == 429
    is_auth_error    = status_code in (401, 402, 403)
    is_request_error = status_code in (400, 404, 413, 422)

    try:
        err_json = response.json()
        if "error" in err_json:
            error_text = err_json["error"].get("message", error_text)
    except Exception:
        pass

    raise ProviderError(
        status_code=status_code,
        message=f"{provider} returned {status_code}: {error_text}",
        is_rate_limit=is_rate_limit,
        is_auth_error=is_auth_error,
        is_request_error=is_request_error,
    )


# ---------------------------------------------------------------------------
# Header building
# ---------------------------------------------------------------------------

def _build_headers(provider: str, api_key: str) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://llm-router.local"
        headers["X-Title"] = "Custom LLM Router"
    return headers


# ---------------------------------------------------------------------------
# Request execution — non-streaming
# ---------------------------------------------------------------------------

async def execute_request(
    provider: str,
    target_model: str,
    api_key: str,
    request_body: Dict[str, Any],
    timeout_override: Optional[float] = None,
    max_retries: int = 2,
    backoff_base: float = 0.5,
) -> httpx.Response:
    """
    Send a non-streaming request to a provider.

    Retries on transient network errors (httpx.RequestError) using exponential
    backoff: delay = backoff_base * 2^attempt  (0.5s, 1s, 2s by default).
    Provider-level errors (4xx/5xx) are NOT retried — the fallback chain in
    main.py handles those via key rotation and provider switching.

    Args:
        timeout_override: Per-request timeout in seconds from X-Request-Timeout header.
        max_retries: Number of *extra* attempts beyond the first (0 = no retry).
        backoff_base: Base seconds for exponential backoff.
    """
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        raise ProviderError(500, f"Unknown provider: {provider}")

    headers = _build_headers(provider, api_key)
    body = {**request_body, "model": target_model}

    client = _get_client()
    timeout = (
        httpx.Timeout(timeout_override)
        if timeout_override
        else PROVIDER_TIMEOUTS.get(provider)
    )

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = backoff_base * (2 ** (attempt - 1))
            logger.debug(
                f"[client] Retry attempt={attempt} provider={provider} delay={delay:.1f}s",
                extra={"event": "retry", "provider": provider, "attempt": attempt}
            )
            await asyncio.sleep(delay)
        try:
            response = await client.post(endpoint, json=body, headers=headers, timeout=timeout)
            if response.status_code != 200:
                _handle_error_response(response, provider)
            return response
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning(
                f"[client] Network error provider={provider} attempt={attempt}: {exc}",
                extra={"event": "network_error", "provider": provider, "attempt": attempt}
            )
            if attempt == max_retries:
                raise ProviderError(500, f"Network error connecting to {provider}: {exc}") from exc
        except ProviderError:
            raise  # Do not retry provider-level errors

    raise ProviderError(500, f"All {max_retries + 1} attempts failed for {provider}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Request execution — streaming
# ---------------------------------------------------------------------------

async def execute_stream_request(
    provider: str,
    target_model: str,
    api_key: str,
    request_body: Dict[str, Any],
    on_complete: Optional[Callable[[int, int], Awaitable[None]]] = None,
    timeout_override: Optional[float] = None,
    max_retries: int = 2,
    backoff_base: float = 0.5,
) -> AsyncGenerator[bytes, None]:
    """
    Send a streaming request and yield SSE chunks.

    Retries are attempted *before* the connection is established (i.e. on
    connection errors). Once the stream has started, it is not retried.

    on_complete(prompt_tokens, completion_tokens) is called after the stream
    ends, so the caller can log accurate token counts without blocking.
    """
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        raise ProviderError(500, f"Unknown provider: {provider}")

    headers = _build_headers(provider, api_key)
    body = {
        **request_body,
        "model": target_model,
        "stream": True,
    }
    body.setdefault("stream_options", {"include_usage": True})

    client = _get_client()
    timeout = (
        httpx.Timeout(timeout_override)
        if timeout_override
        else PROVIDER_TIMEOUTS.get(provider)
    )

    response: Optional[httpx.Response] = None
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = backoff_base * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
        try:
            req = client.build_request("POST", endpoint, json=body, headers=headers, timeout=timeout)
            response = await client.send(req, stream=True)
            break  # Connection established — stop retrying
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt == max_retries:
                raise ProviderError(500, f"Network error connecting to {provider}: {exc}") from exc

    if response is None:  # pragma: no cover
        raise ProviderError(500, f"Failed to connect to {provider}")

    if response.status_code != 200:
        await response.aread()
        _handle_error_response(response, provider)

    async def _generator() -> AsyncGenerator[bytes, None]:
        prompt_tokens = 0
        completion_tokens = 0
        try:
            async for chunk in response.aiter_bytes():  # type: ignore[union-attr]
                yield chunk
                # Parse SSE lines to extract token usage from any chunk
                try:
                    for line in chunk.decode("utf-8", errors="ignore").splitlines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            data = json.loads(line[6:])
                            usage = data.get("usage") or {}
                            if usage:
                                prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                                completion_tokens = usage.get("completion_tokens", completion_tokens)
                except Exception:
                    pass  # Non-fatal: malformed chunk
        finally:
            await response.aclose()  # type: ignore[union-attr]
            if on_complete:
                await on_complete(prompt_tokens, completion_tokens)

    return _generator()
