# src/main.py
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from src.config import settings
from src.schemas import ChatCompletionRequest
from src.core.logging_config import setup_logging, get_logger
from src.core.circuit_breaker import circuit_breaker, CircuitBreaker
from src.core.key_cache import init_key_cache
from src.core.router import resolve_fallback_chain, filter_chain_for_multimodal
from src.core.pool_manager import (
    supabase,
    get_healthy_keys,
    mark_key_cooldown,
    mark_key_dead,
    update_key_last_used,
    log_request,
    restore_all_expired_cooldowns,
    get_client_key_info,
)
from src.providers.client import (
    execute_request,
    execute_stream_request,
    init_http_client,
    close_http_client,
    ProviderError,
)
from src.routers import admin as admin_router

logger = get_logger(__name__)

# ── Background Task: Cooldown Restoration ─────────────────────────────────────
_cooldown_task: Optional[asyncio.Task] = None


async def _periodic_cooldown_restorer():
    """Background loop: periodically restores keys whose cooldown period expired."""
    while True:
        try:
            await asyncio.sleep(settings.COOLDOWN_RESTORE_INTERVAL)
            await restore_all_expired_cooldowns()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[background] Error in cooldown restorer: {e}", exc_info=True)


# ── Lifespan Context Manager ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared resources on startup and clean up on shutdown."""
    setup_logging(settings.LOG_LEVEL)
    init_http_client()
    init_key_cache(settings.KEY_CACHE_TTL)

    # Re-configure circuit breaker thresholds from settings
    circuit_breaker.failure_threshold = settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD
    circuit_breaker.recovery_secs = settings.CIRCUIT_BREAKER_RECOVERY_SECS

    # Start periodic background restoration of expired cooldown keys
    global _cooldown_task
    _cooldown_task = asyncio.create_task(_periodic_cooldown_restorer())
    logger.info("[lifespan] Router initialised with background tasks active",
                extra={"event": "startup", "multi_tenant": settings.MULTI_TENANT})

    yield

    # Shutdown cleanup
    if _cooldown_task:
        _cooldown_task.cancel()
        try:
            await _cooldown_task
        except asyncio.CancelledError:
            pass
    await close_http_client()
    logger.info("[lifespan] Router shutdown complete", extra={"event": "shutdown"})


# ── Rate Limiter ──────────────────────────────────────────────────────────────
def _get_rate_limit_key(request: Request) -> str:
    """Identify caller by Bearer token (per-token quota), falling back to client IP."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return get_remote_address(request)


limiter = Limiter(key_func=_get_rate_limit_key, headers_enabled=True)

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Custom LLM Router",
    version="2.0.0",
    description="Production-grade OpenAI-compatible LLM Gateway with key rotation, multi-tenancy, and circuit breaking",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(admin_router.router)

security = HTTPBearer(auto_error=False)


# ── Auth Middleware ───────────────────────────────────────────────────────────
async def verify_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    """
    Authenticate callers and attach client context to request.state.

    Modes:
      1. Single-tenant (MULTI_TENANT=False): Validates against ROUTER_API_KEY if set.
      2. Multi-tenant  (MULTI_TENANT=True) : Validates token against client_keys table in DB.
    """
    token = credentials.credentials if credentials else None

    if settings.MULTI_TENANT:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Bearer API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        client_info = await get_client_key_info(token)
        if not client_info:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked client API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.client_key_id = client_info["id"]
        request.state.allowed_models = client_info.get("allowed_models")
        return client_info

    # Single-tenant mode
    if settings.ROUTER_API_KEY:
        if not token or token != settings.ROUTER_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing Router API Key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    request.state.client_key_id = None
    request.state.allowed_models = None
    return {}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"message": "LLM Router v2.0 is running. Access /health for status."}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """Returns router status, database status, and real-time key pool snapshot."""
    def _query():
        return supabase.table("provider_keys").select("provider, status").execute()

    pool: dict = {}
    db_ok = True
    try:
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
        "circuit_breakers": circuit_breaker.get_status(),
    }


@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """Return the list of models supported by this router."""
    from src.core.router import get_route_cache

    try:
        routes = await get_route_cache(supabase)
        model_list = []
        for model_id in routes.keys():
            lower_id = model_id.lower()
            if "gemini" in lower_id:
                owned_by = "google"
            elif "claude" in lower_id or "sonnet" in lower_id or "opus" in lower_id:
                owned_by = "anthropic"
            elif "llama" in lower_id:
                owned_by = "meta"
            elif "gpt" in lower_id or lower_id.startswith("o1") or lower_id.startswith("o3"):
                owned_by = "openai"
            elif "kimi" in lower_id or "moonshot" in lower_id:
                owned_by = "moonshotai"
            elif "deepseek" in lower_id:
                owned_by = "deepseek"
            elif "qwen" in lower_id:
                owned_by = "alibaba"
            elif "mistral" in lower_id:
                owned_by = "mistral"
            else:
                owned_by = "custom-router"

            model_list.append({"id": model_id, "object": "model", "owned_by": owned_by})
        return {"object": "list", "data": model_list}
    except Exception as e:
        logger.error(f"[main] Error building models list: {e}", exc_info=True)
        return {"object": "list", "data": []}


@app.get("/v1/quota", dependencies=[Depends(verify_api_key)])
async def get_quota(request: Request):
    """Return rate limit quota, remaining requests, and reset time for caller."""
    from limits import parse

    identity = _get_rate_limit_key(request)
    rpm_item = parse(f"{settings.RATE_LIMIT_RPM}/minute")
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

    masked_identity = identity[:8] + "..." if len(identity) > 12 else identity
    return {
        "identity": masked_identity,
        "rate_limit_minute": {
            "limit": settings.RATE_LIMIT_RPM,
            "remaining": rpm_remaining,
            "reset_seconds": round(rpm_reset_seconds, 2),
            "reset_time": round(rpm_reset_time, 2),
        },
        "rate_limit_burst_second": {
            "limit": settings.RATE_LIMIT_BURST,
            "remaining": burst_remaining,
            "reset_seconds": round(burst_reset_seconds, 2),
            "reset_time": round(burst_reset_time, 2),
        },
    }


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
@limiter.limit(lambda: f"{settings.RATE_LIMIT_RPM}/minute;{settings.RATE_LIMIT_BURST}/second")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """Unified OpenAI-compatible chat completions endpoint with automatic fallback and key rotation."""
    # ── Tracing ID ────────────────────────────────────────────────────────────
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
    client_key_id = getattr(request.state, "client_key_id", None)
    allowed_models = getattr(request.state, "allowed_models", None)

    # ── Model Authorization (Multi-Tenant) ───────────────────────────────────
    requested_model = body.model
    if allowed_models and requested_model not in allowed_models:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Model '{requested_model}' is not allowed for your client API key.",
        )

    # ── Per-Request Timeout ───────────────────────────────────────────────────
    # Header X-Request-Timeout takes precedence over body.timeout
    header_timeout = request.headers.get("X-Request-Timeout")
    timeout_override: Optional[float] = None
    if header_timeout:
        try:
            timeout_override = float(header_timeout)
        except ValueError:
            pass
    elif body.timeout:
        timeout_override = body.timeout

    is_stream = body.stream
    # Convert body to dict (exclude router-specific extensions before sending to provider)
    body_dict = body.model_dump(exclude={"metadata", "timeout"}, exclude_none=True)
    req_metadata = body.metadata  # logged to request_logs

    # ── 1. Resolve Fallback Chain ─────────────────────────────────────────────
    fallback_chain = await resolve_fallback_chain(requested_model, supabase)

    # Semantic routing: if request contains images, prefer vision-capable models
    if body.has_image_content():
        fallback_chain = filter_chain_for_multimodal(fallback_chain)

    errors_log = []

    # ── 2. Iterate Fallback Chain ─────────────────────────────────────────────
    for provider, target_model in fallback_chain:

        # Check circuit breaker — skip provider immediately if circuit is OPEN
        if circuit_breaker.is_open(provider):
            logger.warning(
                f"[router] Skipping provider={provider} (circuit breaker is OPEN)",
                extra={"event": "circuit_skipped", "provider": provider, "request_id": request_id}
            )
            errors_log.append(f"Provider {provider} skipped (circuit breaker is OPEN)")
            continue

        # Fetch healthy keys (uses in-memory cache)
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
                    _key_id = key_id
                    _provider = provider
                    _target_model = target_model
                    _start_time = start_time
                    _req_id = request_id
                    _client_key_id = client_key_id
                    _metadata = req_metadata

                    async def on_stream_complete(prompt_tokens: int, completion_tokens: int):
                        circuit_breaker.record_success(_provider)
                        asyncio.create_task(update_key_last_used(_key_id))
                        asyncio.create_task(log_request(
                            provider=_provider,
                            model=_target_model,
                            key_id=_key_id,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            latency_ms=int((time.time() - _start_time) * 1000),
                            status_code=200,
                            request_id=_req_id,
                            client_key_id=_client_key_id,
                            metadata=_metadata,
                        ))

                    stream_generator = await execute_stream_request(
                        provider=provider,
                        target_model=target_model,
                        api_key=api_key,
                        request_body=body_dict,
                        on_complete=on_stream_complete,
                        timeout_override=timeout_override,
                        max_retries=settings.MAX_RETRIES,
                        backoff_base=settings.RETRY_BACKOFF_BASE,
                    )

                    return StreamingResponse(
                        stream_generator,
                        media_type="text/event-stream",
                        headers={"X-Request-ID": request_id},
                    )
                else:
                    response = await execute_request(
                        provider=provider,
                        target_model=target_model,
                        api_key=api_key,
                        request_body=body_dict,
                        timeout_override=timeout_override,
                        max_retries=settings.MAX_RETRIES,
                        backoff_base=settings.RETRY_BACKOFF_BASE,
                    )

                    circuit_breaker.record_success(provider)
                    response_json = response.json()
                    usage = response_json.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    latency_ms = int((time.time() - start_time) * 1000)

                    asyncio.create_task(update_key_last_used(key_id))
                    asyncio.create_task(log_request(
                        provider=provider,
                        model=target_model,
                        key_id=key_id,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ms=latency_ms,
                        status_code=200,
                        request_id=request_id,
                        client_key_id=client_key_id,
                        metadata=req_metadata,
                    ))

                    return JSONResponse(
                        content=response_json,
                        headers={"X-Request-ID": request_id},
                    )

            except ProviderError as pe:
                latency_ms = int((time.time() - start_time) * 1000)
                error_msg = str(pe)
                errors_log.append(f"Provider {provider} (Key ID: {key_id}) failed: {error_msg}")
                circuit_breaker.record_failure(provider)

                await log_request(
                    provider=provider,
                    model=target_model,
                    key_id=key_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=latency_ms,
                    status_code=pe.status_code,
                    error_message=error_msg,
                    request_id=request_id,
                    client_key_id=client_key_id,
                    metadata=req_metadata,
                )

                if pe.is_rate_limit:
                    await mark_key_cooldown(key_id, duration_seconds=settings.COOLDOWN_RATE_LIMIT_SECS, provider=provider)
                elif pe.is_auth_error:
                    await mark_key_dead(key_id, error_msg, provider=provider)
                elif pe.is_request_error:
                    break  # Client request error (400/404/422) — break key loop, try next provider
                else:
                    await mark_key_cooldown(key_id, duration_seconds=settings.COOLDOWN_NETWORK_ERROR_SECS, provider=provider)

                if pe.is_request_error:
                    break
                continue

            except Exception as e:
                latency_ms = int((time.time() - start_time) * 1000)
                error_msg = f"Unexpected error: {str(e)}"
                errors_log.append(error_msg)
                circuit_breaker.record_failure(provider)

                await log_request(
                    provider=provider,
                    model=target_model,
                    key_id=key_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=latency_ms,
                    status_code=500,
                    error_message=error_msg,
                    request_id=request_id,
                    client_key_id=client_key_id,
                    metadata=req_metadata,
                )

                await mark_key_cooldown(key_id, duration_seconds=settings.COOLDOWN_NETWORK_ERROR_SECS, provider=provider)
                continue

    # All attempts in fallback chain failed
    raise HTTPException(
        status_code=503,
        detail={
            "message": "All LLM routing options in the fallback chain failed.",
            "request_id": request_id,
            "errors": errors_log,
        },
        headers={"X-Request-ID": request_id},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=settings.PORT, reload=True)
