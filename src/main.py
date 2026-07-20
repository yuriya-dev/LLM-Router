import asyncio
import time
from contextlib import asynccontextmanager
import os
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from typing import Dict, Any

from src.config import settings
from src.schemas import ChatCompletionRequest
from src.core.router import resolve_fallback_chain
from src.core.pool_manager import (
    supabase,
    get_healthy_keys,
    mark_key_cooldown,
    mark_key_dead,
    update_key_last_used,
    log_request,
    restore_all_expired_cooldowns
)
from src.providers.client import (
    execute_request,
    execute_stream_request,
    init_http_client,
    close_http_client,
    ProviderError
)
from src.routers import admin as admin_router

def _get_rate_limit_key(request: Request) -> str:
    """
    Identify the caller by their Bearer token (so each API key has its own quota).
    Falls back to IP address if no token is present.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]  # Use the token itself as the identity
    return get_remote_address(request)

# Limiter uses the token/IP as the identity key
# headers_enabled=True automatically adds X-RateLimit-Limit, X-RateLimit-Remaining, and X-RateLimit-Reset headers to the responses
limiter = Limiter(key_func=_get_rate_limit_key, headers_enabled=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources on startup and clean up on shutdown."""
    init_http_client()
    yield
    await close_http_client()

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Custom LLM Router", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "https://admin-yuriyadev.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app|http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(admin_router.router)
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

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    """
    Root endpoint returning basic status. Supported for uptime checks on the root URL.
    """
    return {"message": "LLM Router is running. Access /health for pool status."}

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """
    Returns router status and a real-time snapshot of the key pool health.
    Does NOT require authentication — safe to expose for uptime monitors.
    """
    def _query():
        return supabase.table("provider_keys").select("provider, status").execute()

    pool: dict = {}
    db_ok = True
    try:
        # Automatically restore any keys whose cooldown has expired before checking health
        await restore_all_expired_cooldowns()
        response = await asyncio.to_thread(_query)
        for row in (response.data or []):
            p, s = row["provider"], row["status"]
            pool.setdefault(p, {"healthy": 0, "cooldown": 0, "dead": 0})
            pool[p][s] = pool[p].get(s, 0) + 1
    except Exception as e:
        db_ok = False
        pool = {"error": str(e)}

    total_healthy = sum(v.get("healthy", 0) for v in pool.values() if isinstance(v, dict))
    overall = "healthy" if db_ok and total_healthy > 0 else ("degraded" if db_ok else "unhealthy")

    return {
        "status": overall,
        "timestamp": time.time(),
        "db_connected": db_ok,
        "key_pool": pool,
    }

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """
    Return the list of models supported by this router.
    Dynamically loads all virtual models configured in database and static defaults.
    """
    from src.core.router import get_route_cache
    
    try:
        routes = await get_route_cache(supabase)
        model_list = []
        for model_id in routes.keys():
            # Determine owned_by label based on name heuristic
            owned_by = "custom-router"
            lower_id = model_id.lower()
            if "gemini" in lower_id:
                owned_by = "google"
            elif "claude" in lower_id or "sonnet" in lower_id or "opus" in lower_id:
                owned_by = "anthropic"
            elif "llama" in lower_id:
                owned_by = "meta"
            elif "gpt" in lower_id:
                owned_by = "openai"
            elif "deepseek" in lower_id:
                owned_by = "deepseek"
            elif "mistral" in lower_id:
                owned_by = "mistral"
                
            model_list.append({"id": model_id, "object": "model", "owned_by": owned_by})
        return {"object": "list", "data": model_list}
    except Exception as e:
        # Fallback to a static list if something goes wrong
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

@app.get("/v1/quota", dependencies=[Depends(verify_api_key)])
async def get_quota(request: Request):
    """
    Get the caller's current rate limit quota, remaining requests, and reset time.
    Identifies the caller by Bearer token or client IP, matching the rate limit logic.
    """
    from limits import parse
    import time
    
    identity = _get_rate_limit_key(request)
    
    # Primary Limit: RPM
    rpm_item = parse(f"{settings.RATE_LIMIT_RPM}/minute")
    # Burst Limit: Burst per second
    burst_item = parse(f"{settings.RATE_LIMIT_BURST}/second")
    
    now = time.time()
    
    try:
        rpm_stats = limiter.limiter.get_window_stats(rpm_item, identity)
        rpm_remaining = rpm_stats.remaining
        rpm_reset_seconds = max(0.0, rpm_stats.reset_time - now)
        rpm_reset_time = rpm_stats.reset_time
    except Exception:
        rpm_remaining = settings.RATE_LIMIT_RPM
        rpm_reset_seconds = 0.0
        rpm_reset_time = now

    try:
        burst_stats = limiter.limiter.get_window_stats(burst_item, identity)
        burst_remaining = burst_stats.remaining
        burst_reset_seconds = max(0.0, burst_stats.reset_time - now)
        burst_reset_time = burst_stats.reset_time
    except Exception:
        burst_remaining = settings.RATE_LIMIT_BURST
        burst_reset_seconds = 0.0
        burst_reset_time = now
        
    # Mask identity for security if it's a bearer token
    masked_identity = identity
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        masked_identity = identity[:8] + "..." if len(identity) > 12 else "token"

    return {
        "identity": masked_identity,
        "rate_limit_minute": {
            "limit": settings.RATE_LIMIT_RPM,
            "remaining": rpm_remaining,
            "reset_seconds": round(rpm_reset_seconds, 2),
            "reset_time": round(rpm_reset_time, 2)
        },
        "rate_limit_burst_second": {
            "limit": settings.RATE_LIMIT_BURST,
            "remaining": burst_remaining,
            "reset_seconds": round(burst_reset_seconds, 2),
            "reset_time": round(burst_reset_time, 2)
        }
    }

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
@limiter.limit(lambda: f"{settings.RATE_LIMIT_RPM}/minute;{settings.RATE_LIMIT_BURST}/second")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """
    Unified OpenAI-compatible chat completions endpoint with automatic fallback and key rotation.
    """
    requested_model = body.model
    is_stream = body.stream
    # Convert to plain dict for passing to provider HTTP clients
    body_dict = body.model_dump(exclude_none=True)

    # 1. Resolve fallback chain: list of (provider, target_model)
    fallback_chain = await resolve_fallback_chain(requested_model, supabase)
    errors_log = []

    # 2. Iterate through the fallback chain
    for provider, target_model in fallback_chain:
        # Fetch healthy keys for this provider
        keys = await get_healthy_keys(provider)
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
                    # Capture variables for the callback closure
                    _key_id = key_id
                    _provider = provider
                    _target_model = target_model
                    _start_time = start_time

                    async def on_stream_complete(prompt_tokens: int, completion_tokens: int):
                        """Called by the generator after stream ends with accurate token counts."""
                        asyncio.create_task(update_key_last_used(_key_id))
                        asyncio.create_task(log_request(
                            provider=_provider,
                            model=_target_model,
                            key_id=_key_id,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            latency_ms=int((time.time() - _start_time) * 1000),
                            status_code=200
                        ))

                    # Execute streaming request with on_complete callback for accurate token logging
                    stream_generator = await execute_stream_request(
                        provider=provider,
                        target_model=target_model,
                        api_key=api_key,
                        request_body=body_dict,
                        on_complete=on_stream_complete
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
                        request_body=body_dict
                    )
                    
                    response_json = response.json()
                    
                    # Extract token usage if present
                    usage = response_json.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    latency_ms = int((time.time() - start_time) * 1000)

                    # Fire-and-forget: update DB & log in background so client gets response immediately
                    asyncio.create_task(update_key_last_used(key_id))
                    asyncio.create_task(log_request(
                        provider=provider,
                        model=target_model,
                        key_id=key_id,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ms=latency_ms,
                        status_code=200
                    ))
                    
                    return JSONResponse(content=response_json)

            except ProviderError as pe:
                latency_ms = int((time.time() - start_time) * 1000)
                error_msg = str(pe)
                errors_log.append(f"Provider {provider} (Key ID: {key_id}) failed: {error_msg}")
                
                # Log error in database
                await log_request(
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
                    await mark_key_cooldown(key_id, duration_seconds=settings.COOLDOWN_RATE_LIMIT_SECS)
                elif pe.is_auth_error:
                    await mark_key_dead(key_id, error_msg)
                elif pe.is_request_error:
                    # Request-level error (e.g., 400, 404, 413). Do NOT cooldown or mark key dead.
                    # Immediately break the key loop to fallback to next provider.
                    break
                else:
                    # Short cooldown for other failures (network, server errors)
                    await mark_key_cooldown(key_id, duration_seconds=settings.COOLDOWN_NETWORK_ERROR_SECS)
                
                if pe.is_request_error:
                    break
                # Continue loop to try next key
                continue

            except Exception as e:
                latency_ms = int((time.time() - start_time) * 1000)
                error_msg = f"Unexpected error: {str(e)}"
                errors_log.append(error_msg)
                
                await log_request(
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
                await mark_key_cooldown(key_id, duration_seconds=settings.COOLDOWN_NETWORK_ERROR_SECS)
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
