# src/routers/admin.py
"""
Admin API endpoints — protected by ROUTER_API_KEY.

New in this version:
  - POST /admin/keys now auto-encrypts the key_value if ENCRYPTION_KEY is set.
  - GET  /admin/circuit-breakers  — view per-provider circuit breaker status.
  - POST /admin/circuit-breakers/{provider}/reset — manually close a circuit.
  - GET  /admin/client-keys       — list multi-tenant client keys (no hash revealed).
  - POST /admin/client-keys       — create a client key (returned once; only hash stored).
  - DELETE /admin/client-keys/{key_id} — revoke a client key.
  - POST /admin/cache/invalidate  — force-invalidate the in-memory key pool cache.
"""
import asyncio
import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from src.config import settings
from src.core.pool_manager import supabase, create_client_key
from src.core.router import refresh_route_cache
from src.core.circuit_breaker import circuit_breaker
from src.core.key_cache import get_key_cache
from src.core.crypto import encrypt_key
from src.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBearer(auto_error=False)


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_admin_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Admin endpoints always require ROUTER_API_KEY (single key, not multi-tenant)."""
    if settings.ROUTER_API_KEY:
        if not credentials or credentials.credentials != settings.ROUTER_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing Router API Key",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return True


# ── Request / Response Schemas ────────────────────────────────────────────────

class KeyCreateRequest(BaseModel):
    provider: str = Field(..., description="Provider name, e.g. 'gemini', 'groq', 'dashscope'")
    key_name: Optional[str] = Field(None, description="Human-readable label")
    key_value: str = Field(..., min_length=10, description="The raw provider API key")
    priority: int = Field(1, ge=1, description="Lower = higher priority")


class KeyResetRequest(BaseModel):
    status: str = Field("healthy", pattern="^(healthy|cooldown|dead)$")


class KeyUpdateRequest(BaseModel):
    provider: Optional[str] = None
    key_name: Optional[str] = None
    key_value: Optional[str] = Field(None, min_length=10)
    priority: Optional[int] = Field(None, ge=1)
    status: Optional[str] = Field(None, pattern="^(healthy|cooldown|dead)$")


class RouteCreateRequest(BaseModel):
    virtual_model: str = Field(..., description="Virtual model alias, e.g. 'combo-smart'")
    provider: str = Field(..., description="Target provider")
    target_model: str = Field(..., description="Actual model name sent to provider")
    priority: int = Field(1, ge=1)
    enabled: bool = True


class RouteUpdateRequest(BaseModel):
    virtual_model: Optional[str] = None
    provider: Optional[str] = None
    target_model: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1)
    enabled: Optional[bool] = None


class ClientKeyCreateRequest(BaseModel):
    key_name: str = Field(..., description="Human-readable label for this client key")
    allowed_models: Optional[List[str]] = Field(
        None, description="Whitelist of model names. Null = all models allowed."
    )
    daily_token_limit: Optional[int] = Field(
        None, gt=0, description="Maximum tokens per day. Null = unlimited."
    )


# ── Provider Keys ─────────────────────────────────────────────────────────────

@router.get("/keys", dependencies=[Depends(verify_admin_key)])
async def list_keys(
    provider: Optional[str] = Query(None, description="Filter by provider"),
    key_status: Optional[str] = Query(None, alias="status", description="Filter by status"),
):
    """List all provider keys (key_value is never returned for security)."""
    def _query():
        q = supabase.table("provider_keys")\
            .select("id, provider, key_name, status, cooldown_until, priority, error_count, last_used_at, created_at")\
            .order("provider").order("priority")
        if provider:
            q = q.eq("provider", provider)
        if key_status:
            q = q.eq("status", key_status)
        return q.execute()

    try:
        response = await asyncio.to_thread(_query)
        return {"keys": response.data, "total": len(response.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.post("/keys", status_code=201, dependencies=[Depends(verify_admin_key)])
async def create_key(payload: KeyCreateRequest):
    """
    Add a new provider API key.  The key_value is encrypted at rest if
    ENCRYPTION_KEY is configured; stored as plaintext otherwise.
    """
    stored_value = encrypt_key(payload.key_value)

    def _insert():
        return supabase.table("provider_keys").insert({
            "provider": payload.provider,
            "key_name": payload.key_name,
            "key_value": stored_value,
            "priority": payload.priority,
            "status": "healthy",
        }).execute()

    try:
        response = await asyncio.to_thread(_insert)
        # Invalidate cache so next request picks up the new key
        get_key_cache().invalidate(payload.provider)
        row = response.data[0] if response.data else {}
        # Never return key_value
        row.pop("key_value", None)
        return {"message": "Key created", "key": row}
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="A key with this value already exists.")
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.patch("/keys/{key_id}", dependencies=[Depends(verify_admin_key)])
async def update_key(key_id: str, payload: KeyUpdateRequest):
    """Update an existing provider key."""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")
    # Encrypt key_value if it's being updated
    if "key_value" in update_data:
        update_data["key_value"] = encrypt_key(update_data["key_value"])

    def _update():
        return supabase.table("provider_keys").update(update_data).eq("id", key_id).execute()

    try:
        response = await asyncio.to_thread(_update)
        if not response.data:
            raise HTTPException(status_code=404, detail="Key not found")
        row = response.data[0]
        provider = row.get("provider")
        get_key_cache().invalidate(provider)
        row.pop("key_value", None)
        return {"message": "Key updated", "key": row}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.patch("/keys/{key_id}/reset", dependencies=[Depends(verify_admin_key)])
async def reset_key(key_id: str, payload: KeyResetRequest):
    """Reset a key's status back to healthy (or any status)."""
    def _update():
        return supabase.table("provider_keys").update({
            "status": payload.status,
            "cooldown_until": None,
            "error_count": 0,
        }).eq("id", key_id).execute()

    try:
        response = await asyncio.to_thread(_update)
        if not response.data:
            raise HTTPException(status_code=404, detail="Key not found")
        row = response.data[0]
        get_key_cache().invalidate(row.get("provider"))
        row.pop("key_value", None)
        return {"message": f"Key reset to '{payload.status}'", "key": row}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.delete("/keys/{key_id}", dependencies=[Depends(verify_admin_key)])
async def delete_key(key_id: str):
    """Permanently delete a provider key."""
    def _delete():
        return supabase.table("provider_keys").delete().eq("id", key_id).execute()

    try:
        response = await asyncio.to_thread(_delete)
        if not response.data:
            raise HTTPException(status_code=404, detail="Key not found")
        provider = response.data[0].get("provider")
        get_key_cache().invalidate(provider)
        return {"message": "Key deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats", dependencies=[Depends(verify_admin_key)])
async def get_stats(hours: int = Query(24, ge=1, le=720)):
    """Aggregated request statistics grouped by provider/model for the last N hours."""
    since = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    ).isoformat()

    def _query():
        return supabase.table("request_logs")\
            .select("provider, model, status_code, prompt_tokens, completion_tokens, latency_ms")\
            .gte("created_at", since)\
            .execute()

    try:
        response = await asyncio.to_thread(_query)
        rows = response.data or []
        agg: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = f"{row['provider']}/{row['model']}"
            if key not in agg:
                agg[key] = {
                    "provider": row["provider"], "model": row["model"],
                    "total_requests": 0, "success_requests": 0, "error_requests": 0,
                    "total_prompt_tokens": 0, "total_completion_tokens": 0,
                    "avg_latency_ms": 0, "_latency_sum": 0,
                }
            e = agg[key]
            e["total_requests"] += 1
            if row["status_code"] == 200:
                e["success_requests"] += 1
            else:
                e["error_requests"] += 1
            e["total_prompt_tokens"] += row.get("prompt_tokens") or 0
            e["total_completion_tokens"] += row.get("completion_tokens") or 0
            e["_latency_sum"] += row.get("latency_ms") or 0

        for e in agg.values():
            n = e["total_requests"]
            e["avg_latency_ms"] = round(e.pop("_latency_sum") / n) if n else 0

        return {
            "window_hours": hours, "since": since,
            "stats": sorted(agg.values(), key=lambda x: x["total_requests"], reverse=True),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.get("/status", dependencies=[Depends(verify_admin_key)])
async def pool_status():
    """Quick pool health snapshot: key counts by provider and status."""
    def _query():
        return supabase.table("provider_keys").select("provider, status").execute()

    try:
        response = await asyncio.to_thread(_query)
        summary: Dict[str, Dict[str, int]] = {}
        for row in (response.data or []):
            p, s = row["provider"], row["status"]
            summary.setdefault(p, {"healthy": 0, "cooldown": 0, "dead": 0})
            summary[p][s] = summary[p].get(s, 0) + 1
        return {"pool": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ── Circuit Breakers ──────────────────────────────────────────────────────────

@router.get("/circuit-breakers", dependencies=[Depends(verify_admin_key)])
async def get_circuit_breakers():
    """View the current state of all per-provider circuit breakers."""
    return {"circuit_breakers": circuit_breaker.get_status()}


@router.post("/circuit-breakers/{provider}/reset", dependencies=[Depends(verify_admin_key)])
async def reset_circuit_breaker(provider: str):
    """Manually close (reset) a provider's circuit breaker."""
    found = circuit_breaker.reset(provider)
    if not found:
        return {"message": f"No circuit breaker state found for provider '{provider}' (already clean)"}
    return {"message": f"Circuit breaker for '{provider}' has been reset to CLOSED"}


# ── Key Pool Cache ────────────────────────────────────────────────────────────

@router.post("/cache/invalidate", dependencies=[Depends(verify_admin_key)])
async def invalidate_cache(provider: Optional[str] = Query(None, description="Provider to invalidate, or all if omitted")):
    """Force-invalidate the in-memory key pool cache."""
    get_key_cache().invalidate(provider)
    target = f"provider={provider}" if provider else "ALL providers"
    return {"message": f"Key pool cache invalidated for {target}"}


# ── Model Routes ──────────────────────────────────────────────────────────────

@router.post("/routes", status_code=201, dependencies=[Depends(verify_admin_key)])
async def create_route(payload: RouteCreateRequest):
    """Add a new model route to the dynamic routing table."""
    def _insert():
        return supabase.table("model_routes").insert({
            "virtual_model": payload.virtual_model,
            "provider": payload.provider,
            "target_model": payload.target_model,
            "priority": payload.priority,
            "enabled": payload.enabled,
        }).execute()

    try:
        response = await asyncio.to_thread(_insert)
        await refresh_route_cache(supabase)
        return {"message": "Route created and cache refreshed", "route": response.data[0] if response.data else {}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.get("/routes", dependencies=[Depends(verify_admin_key)])
async def list_routes():
    """List all model routes from the dynamic routing table."""
    def _query():
        return supabase.table("model_routes")\
            .select("*").order("virtual_model").order("priority").execute()

    try:
        response = await asyncio.to_thread(_query)
        return {"routes": response.data, "total": len(response.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.patch("/routes/{route_id}", dependencies=[Depends(verify_admin_key)])
async def update_route(route_id: str, payload: RouteUpdateRequest):
    """Update an existing model route and refresh cache."""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    def _update():
        return supabase.table("model_routes").update(update_data).eq("id", route_id).execute()

    try:
        response = await asyncio.to_thread(_update)
        if not response.data:
            raise HTTPException(status_code=404, detail="Route not found")
        await refresh_route_cache(supabase)
        return {"message": "Route updated and cache refreshed", "route": response.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.delete("/routes/{route_id}", dependencies=[Depends(verify_admin_key)])
async def delete_route(route_id: str):
    """Delete a model route and refresh cache."""
    def _delete():
        return supabase.table("model_routes").delete().eq("id", route_id).execute()

    try:
        response = await asyncio.to_thread(_delete)
        if not response.data:
            raise HTTPException(status_code=404, detail="Route not found")
        await refresh_route_cache(supabase)
        return {"message": "Route deleted and cache refreshed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.post("/routes/refresh", dependencies=[Depends(verify_admin_key)])
async def force_refresh_routes():
    """Force-reload the route cache from Supabase immediately."""
    try:
        await refresh_route_cache(supabase)
        return {"message": "Route cache refreshed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to refresh routes: {e}")


# ── Multi-Tenant Client Keys ──────────────────────────────────────────────────

@router.get("/client-keys", dependencies=[Depends(verify_admin_key)])
async def list_client_keys():
    """List all multi-tenant client keys (key hash is never exposed)."""
    def _query():
        return supabase.table("client_keys")\
            .select("id, key_name, is_active, allowed_models, daily_token_limit, created_at")\
            .order("created_at", desc=True)\
            .execute()

    try:
        response = await asyncio.to_thread(_query)
        return {"client_keys": response.data, "total": len(response.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.post("/client-keys", status_code=201, dependencies=[Depends(verify_admin_key)])
async def create_client_key_endpoint(payload: ClientKeyCreateRequest):
    """
    Create a new multi-tenant client API key.
    The plaintext key is returned ONCE.  Only its SHA-256 hash is stored.
    """
    try:
        result = await create_client_key(
            key_name=payload.key_name,
            allowed_models=payload.allowed_models,
            daily_token_limit=payload.daily_token_limit,
        )
        return {
            "message": "Client key created. Store the key securely — it will not be shown again.",
            "client_key": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@router.delete("/client-keys/{key_id}", dependencies=[Depends(verify_admin_key)])
async def revoke_client_key(key_id: str):
    """Revoke (soft-delete) a client key by setting is_active=false."""
    def _update():
        return supabase.table("client_keys")\
            .update({"is_active": False})\
            .eq("id", key_id)\
            .execute()

    try:
        response = await asyncio.to_thread(_update)
        if not response.data:
            raise HTTPException(status_code=404, detail="Client key not found")
        return {"message": "Client key revoked"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
