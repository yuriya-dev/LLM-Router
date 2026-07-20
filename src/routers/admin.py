# src/routers/admin.py
import asyncio
import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from src.config import settings
from src.core.pool_manager import supabase
from src.core.router import refresh_route_cache

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBearer(auto_error=False)


def verify_admin_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Admin endpoints require ROUTER_API_KEY (same key as the main router).
    If no key is configured the server is assumed to be in a private network.
    """
    if settings.ROUTER_API_KEY:
        if not credentials or credentials.credentials != settings.ROUTER_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing Router API Key",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return True


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------

class KeyCreateRequest(BaseModel):
    provider: str = Field(..., description="Provider name: 'gemini', 'groq', 'openrouter'")
    key_name: Optional[str] = Field(None, description="Human-readable label")
    key_value: str = Field(..., min_length=10, description="The actual API key")
    priority: int = Field(1, ge=1, description="Lower = higher priority")


class KeyResetRequest(BaseModel):
    status: str = Field("healthy", pattern="^(healthy|cooldown|dead)$")


class KeyUpdateRequest(BaseModel):
    provider: Optional[str] = None
    key_name: Optional[str] = None
    key_value: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1)
    status: Optional[str] = Field(None, pattern="^(healthy|cooldown|dead)$")


class RouteCreateRequest(BaseModel):
    virtual_model: str = Field(..., description="Virtual model alias (e.g. 'combo-smart')")
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


# ---------------------------------------------------------------------------
# GET /admin/keys  — list all keys with their current status
# ---------------------------------------------------------------------------

@router.get("/keys", dependencies=[Depends(verify_admin_key)])
async def list_keys(
    provider: Optional[str] = Query(None, description="Filter by provider"),
    key_status: Optional[str] = Query(None, alias="status", description="Filter by status"),
):
    """Return all provider keys with their current status and metrics."""
    def _query():
        q = supabase.table("provider_keys")\
            .select("id, provider, key_name, status, cooldown_until, priority, error_count, last_used_at, created_at")\
            .order("provider")\
            .order("priority")
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


# ---------------------------------------------------------------------------
# POST /admin/keys  — add a new API key
# ---------------------------------------------------------------------------

@router.post("/keys", status_code=201, dependencies=[Depends(verify_admin_key)])
async def create_key(payload: KeyCreateRequest):
    """Add a new provider API key to the pool."""
    def _insert():
        return supabase.table("provider_keys").insert({
            "provider": payload.provider,
            "key_name": payload.key_name,
            "key_value": payload.key_value,
            "priority": payload.priority,
            "status": "healthy",
        }).execute()

    try:
        response = await asyncio.to_thread(_insert)
        return {"message": "Key created", "key": response.data[0] if response.data else {}}
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="A key with this value already exists.")
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# PATCH /admin/keys/{key_id}  — update details of an existing key
# ---------------------------------------------------------------------------

@router.patch("/keys/{key_id}", dependencies=[Depends(verify_admin_key)])
async def update_key(key_id: str, payload: KeyUpdateRequest):
    """Update details of an existing provider key."""
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    def _update():
        return supabase.table("provider_keys").update(update_data).eq("id", key_id).execute()

    try:
        response = await asyncio.to_thread(_update)
        if not response.data:
            raise HTTPException(status_code=404, detail="Key not found")
        return {"message": "Key updated", "key": response.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# PATCH /admin/keys/{key_id}/reset  — reset a key back to healthy (or any status)
# ---------------------------------------------------------------------------

@router.patch("/keys/{key_id}/reset", dependencies=[Depends(verify_admin_key)])
async def reset_key(key_id: str, payload: KeyResetRequest):
    """Reset a key's status (e.g. bring a 'dead' key back to 'healthy')."""
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
        return {"message": f"Key reset to '{payload.status}'", "key": response.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# DELETE /admin/keys/{key_id}  — permanently remove a key
# ---------------------------------------------------------------------------

@router.delete("/keys/{key_id}", dependencies=[Depends(verify_admin_key)])
async def delete_key(key_id: str):
    """Permanently delete a key from the pool."""
    def _delete():
        return supabase.table("provider_keys").delete().eq("id", key_id).execute()

    try:
        response = await asyncio.to_thread(_delete)
        if not response.data:
            raise HTTPException(status_code=404, detail="Key not found")
        return {"message": "Key deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# GET /admin/stats  — usage summary per provider / model
# ---------------------------------------------------------------------------

@router.get("/stats", dependencies=[Depends(verify_admin_key)])
async def get_stats(
    hours: int = Query(24, ge=1, le=720, description="Look-back window in hours"),
):
    """Return aggregated request statistics grouped by provider and model."""
    since = (
        datetime.datetime.now(datetime.timezone.utc) -
        datetime.timedelta(hours=hours)
    ).isoformat()

    def _query():
        return supabase.table("request_logs")\
            .select("provider, model, status_code, prompt_tokens, completion_tokens, latency_ms")\
            .gte("created_at", since)\
            .execute()

    try:
        response = await asyncio.to_thread(_query)
        rows = response.data or []

        # Aggregate in Python (avoids need for a Supabase RPC/view)
        agg: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = f"{row['provider']}/{row['model']}"
            if key not in agg:
                agg[key] = {
                    "provider": row["provider"],
                    "model": row["model"],
                    "total_requests": 0,
                    "success_requests": 0,
                    "error_requests": 0,
                    "total_prompt_tokens": 0,
                    "total_completion_tokens": 0,
                    "avg_latency_ms": 0,
                    "_latency_sum": 0,
                }
            entry = agg[key]
            entry["total_requests"] += 1
            if row["status_code"] == 200:
                entry["success_requests"] += 1
            else:
                entry["error_requests"] += 1
            entry["total_prompt_tokens"] += row.get("prompt_tokens") or 0
            entry["total_completion_tokens"] += row.get("completion_tokens") or 0
            entry["_latency_sum"] += row.get("latency_ms") or 0

        # Compute averages and clean up internal keys
        for entry in agg.values():
            n = entry["total_requests"]
            entry["avg_latency_ms"] = round(entry.pop("_latency_sum") / n) if n else 0

        return {
            "window_hours": hours,
            "since": since,
            "stats": sorted(agg.values(), key=lambda x: x["total_requests"], reverse=True),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# GET /admin/status  — quick pool health snapshot
# ---------------------------------------------------------------------------

@router.get("/status", dependencies=[Depends(verify_admin_key)])
async def pool_status():
    """Return a quick health snapshot: count of keys by provider and status."""
    def _query():
        return supabase.table("provider_keys")\
            .select("provider, status")\
            .execute()

    try:
        response = await asyncio.to_thread(_query)
        rows = response.data or []

        summary: Dict[str, Dict[str, int]] = {}
        for row in rows:
            p = row["provider"]
            s = row["status"]
            summary.setdefault(p, {"healthy": 0, "cooldown": 0, "dead": 0})
            summary[p][s] = summary[p].get(s, 0) + 1

        return {"pool": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# POST /admin/routes  — add a new model route
# ---------------------------------------------------------------------------

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
        # Invalidate the route cache immediately
        await refresh_route_cache(supabase)
        return {"message": "Route created and cache refreshed", "route": response.data[0] if response.data else {}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# GET /admin/routes  — list all routes
# ---------------------------------------------------------------------------

@router.get("/routes", dependencies=[Depends(verify_admin_key)])
async def list_routes():
    """List all model routes from the dynamic routing table."""
    def _query():
        return supabase.table("model_routes")\
            .select("*")\
            .order("virtual_model")\
            .order("priority")\
            .execute()

    try:
        response = await asyncio.to_thread(_query)
        return {"routes": response.data, "total": len(response.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ---------------------------------------------------------------------------
# PATCH /admin/routes/{route_id}  — update a route
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# DELETE /admin/routes/{route_id}  — delete a route
# ---------------------------------------------------------------------------

@router.delete("/routes/{route_id}", dependencies=[Depends(verify_admin_key)])
async def delete_route(route_id: str):
    """Delete a model route from dynamic routing table and refresh cache."""
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


# ---------------------------------------------------------------------------
# POST /admin/routes/refresh  — force reload route cache from DB
# ---------------------------------------------------------------------------

@router.post("/routes/refresh", dependencies=[Depends(verify_admin_key)])
async def force_refresh_routes():
    """Force-reload the route cache from Supabase immediately."""
    try:
        await refresh_route_cache(supabase)
        return {"message": "Route cache refreshed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to refresh routes: {e}")

