# src/providers/client.py
import time
import httpx
from typing import Dict, Any, AsyncGenerator
from fastapi import HTTPException

PROVIDER_ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}

class ProviderError(Exception):
    def __init__(self, status_code: int, message: str, is_rate_limit: bool = False, is_auth_error: bool = False):
        self.status_code = status_code
        self.message = message
        self.is_rate_limit = is_rate_limit
        self.is_auth_error = is_auth_error
        super().__init__(message)

async def execute_request(
    provider: str,
    target_model: str,
    api_key: str,
    request_body: Dict[str, Any]
) -> httpx.Response:
    """
    Sends a non-streaming request to the provider.
    """
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        raise ProviderError(500, f"Unknown provider: {provider}")

    # Prepare headers
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://9router-custom.local"
        headers["X-Title"] = "Custom LLM Router"

    # Deep copy/modify request body
    body = dict(request_body)
    body["model"] = target_model

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(endpoint, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise ProviderError(500, f"Network error connecting to {provider}: {str(exc)}")
            
        if response.status_code != 200:
            _handle_error_response(response, provider)
            
        return response

async def execute_stream_request(
    provider: str,
    target_model: str,
    api_key: str,
    request_body: Dict[str, Any]
) -> AsyncGenerator[bytes, None]:
    """
    Sends a streaming request and yields chunks.
    """
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        raise ProviderError(500, f"Unknown provider: {provider}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://9router-custom.local"
        headers["X-Title"] = "Custom LLM Router"

    body = dict(request_body)
    body["model"] = target_model
    body["stream"] = True

    client = httpx.AsyncClient(timeout=60.0)
    
    try:
        # We handle stream opening
        request = client.build_request("POST", endpoint, json=body, headers=headers)
        response = await client.send(request, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        raise ProviderError(500, f"Network error connecting to {provider}: {str(exc)}")

    if response.status_code != 200:
        # Read the error content and handle it
        await response.aread()
        await client.aclose()
        _handle_error_response(response, provider)

    async def response_generator() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return response_generator()

def _handle_error_response(response: httpx.Response, provider: str):
    status_code = response.status_code
    error_text = response.text
    
    is_rate_limit = status_code == 429
    is_auth_error = status_code in (401, 403)
    
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
        is_auth_error=is_auth_error
    )
