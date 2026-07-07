# src/main.py
import time
import json
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Dict, Any

from src.config import settings
from src.core.router import resolve_fallback_chain
from src.core.pool_manager import (
    get_healthy_keys,
    mark_key_cooldown,
    mark_key_dead,
    update_key_last_used,
    log_request
)
from src.providers.client import execute_request, execute_stream_request, ProviderError

app = FastAPI(title="Custom LLM Router", version="1.0.0")
security = HTTPBearer(auto_error=False)

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Optional authentication middleware to protect the router itself.
    """
    if settings.ROUTER_API_KEY:
        if not credentials or credentials.credentials != settings.ROUTER_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing Router API Key",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return True

@app.get("/health")
def health_check():
    return {"status": "healthy", "timestamp": time.time()}

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
def list_models():
    """
    Return the list of models supported by this router.
    """
    return {
        "object": "list",
        "data": [
            {"id": "combo-smart", "object": "model", "owned_by": "custom-router"},
            {"id": "combo-fast", "object": "model", "owned_by": "custom-router"},
            {"id": "gemini-1.5-pro", "object": "model", "owned_by": "google"},
            {"id": "gemini-1.5-flash", "object": "model", "owned_by": "google"},
            {"id": "claude-3-5-sonnet", "object": "model", "owned_by": "anthropic"},
            {"id": "llama3-8b", "object": "model", "owned_by": "meta"},
        ]
    }

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(request: Request):
    """
    Unified OpenAI-compatible chat completions endpoint with automatic fallback and key rotation.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    requested_model = body.get("model")
    if not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")

    is_stream = body.get("stream", False)

    # 1. Resolve fallback chain: list of (provider, target_model)
    fallback_chain = resolve_fallback_chain(requested_model)
    errors_log = []

    # 2. Iterate through the fallback chain
    for provider, target_model in fallback_chain:
        # Fetch healthy keys for this provider
        keys = get_healthy_keys(provider)
        if not keys:
            errors_log.append(f"No healthy keys available for provider: {provider}")
            continue

        # Try each key for the current provider
        for key_info in keys:
            key_id = key_info["id"]
            api_key = key_info["key_value"]
            
            start_time = time.time()
            
            try:
                if is_stream:
                    # Execute streaming request
                    stream_generator = await execute_stream_request(
                        provider=provider,
                        target_model=target_model,
                        api_key=api_key,
                        request_body=body
                    )
                    
                    # Update key last used immediately to maintain LRU/Round-robin
                    update_key_last_used(key_id)
                    
                    # Log successful request trigger in background (0 tokens initially for stream)
                    log_request(
                        provider=provider,
                        model=target_model,
                        key_id=key_id,
                        prompt_tokens=0,
                        completion_tokens=0,
                        latency_ms=int((time.time() - start_time) * 1000),
                        status_code=200
                    )
                    
                    return StreamingResponse(
                        stream_generator,
                        media_type="text/event-stream"
                    )
                else:
                    # Execute non-streaming request
                    response = await execute_request(
                        provider=provider,
                        target_model=target_model,
                        api_key=api_key,
                        request_body=body
                    )
                    
                    response_json = response.json()
                    
                    # Extract token usage if present
                    usage = response_json.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    latency_ms = int((time.time() - start_time) * 1000)

                    # Update database metrics
                    update_key_last_used(key_id)
                    log_request(
                        provider=provider,
                        model=target_model,
                        key_id=key_id,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ms=latency_ms,
                        status_code=200
                    )
                    
                    return JSONResponse(content=response_json)

            except ProviderError as pe:
                latency_ms = int((time.time() - start_time) * 1000)
                error_msg = str(pe)
                errors_log.append(f"Provider {provider} (Key ID: {key_id}) failed: {error_msg}")
                
                # Log error in database
                log_request(
                    provider=provider,
                    model=target_model,
                    key_id=key_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=latency_ms,
                    status_code=pe.status_code,
                    error_message=error_msg
                )
                
                # Update key status based on failure type
                if pe.is_rate_limit:
                    mark_key_cooldown(key_id, duration_seconds=60)
                elif pe.is_auth_error:
                    mark_key_dead(key_id, error_msg)
                else:
                    # Short cooldown for other failures (network, server errors)
                    mark_key_cooldown(key_id, duration_seconds=15)
                
                # Continue loop to try next key
                continue

            except Exception as e:
                latency_ms = int((time.time() - start_time) * 1000)
                error_msg = f"Unexpected error: {str(e)}"
                errors_log.append(error_msg)
                
                log_request(
                    provider=provider,
                    model=target_model,
                    key_id=key_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=latency_ms,
                    status_code=500,
                    error_message=error_msg
                )
                
                # Put in short cooldown on unexpected exception
                mark_key_cooldown(key_id, duration_seconds=15)
                continue

    # If we exited the loops without returning a response, it means all attempts failed
    raise HTTPException(
        status_code=503,
        detail={
            "message": "All LLM routing options in the fallback chain failed.",
            "errors": errors_log
        }
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=settings.PORT, reload=True)
