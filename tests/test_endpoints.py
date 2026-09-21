# tests/test_endpoints.py
import sys
from unittest.mock import MagicMock

# Provide dummy supabase module before imports
sys.modules.setdefault("supabase", MagicMock())

from fastapi.testclient import TestClient
from src.main import app
from src.config import settings

client = TestClient(app)

def test_quota_endpoint():
    # If a router api key is configured, use it. Otherwise, use a mock token.
    headers = {}
    if settings.ROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {settings.ROUTER_API_KEY}"
    else:
        headers["Authorization"] = "Bearer test-token-123"

    response = client.get("/v1/quota", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "identity" in data
    assert "rate_limit_minute" in data
    assert "rate_limit_burst_second" in data
    
    # Check minute limit stats
    minute_stats = data["rate_limit_minute"]
    assert minute_stats["limit"] == settings.RATE_LIMIT_RPM
    assert "remaining" in minute_stats
    assert "reset_seconds" in minute_stats
    assert "reset_time" in minute_stats
    assert isinstance(minute_stats["remaining"], int)
    assert isinstance(minute_stats["reset_seconds"], (int, float))
    assert isinstance(minute_stats["reset_time"], (int, float))

    # Check burst/second limit stats
    burst_stats = data["rate_limit_burst_second"]
    assert burst_stats["limit"] == settings.RATE_LIMIT_BURST
    assert "remaining" in burst_stats
    assert "reset_seconds" in burst_stats
    assert "reset_time" in burst_stats
    assert isinstance(burst_stats["remaining"], int)
    assert isinstance(burst_stats["reset_seconds"], (int, float))
    assert isinstance(burst_stats["reset_time"], (int, float))


def test_models_endpoint():
    headers = {"Authorization": f"Bearer {settings.ROUTER_API_KEY}"} if settings.ROUTER_API_KEY else {"Authorization": "Bearer test"}
    response = client.get("/v1/models", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    model_ids = [m["id"] for m in data["data"]]
    assert "deepseek-v4.1-flash" in model_ids
    deepseek_model = next(m for m in data["data"] if m["id"] == "deepseek-v4.1-flash")
    assert deepseek_model["owned_by"] == "deepseek"

    assert "qwen3.8-max" in model_ids
    qwen_model = next(m for m in data["data"] if m["id"] == "qwen3.8-max")
    assert qwen_model["owned_by"] == "alibaba"


def test_multimodal_chat_completion_schema():
    from src.schemas import ChatCompletionRequest
    payload = {
        "model": "deepseek-v4.1-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
                    {"type": "text", "text": "Please only output the text content in the image."},
                ],
            }
        ],
    }
    req = ChatCompletionRequest(**payload)
    assert req.model == "deepseek-v4.1-flash"
    assert len(req.messages) == 1
    assert isinstance(req.messages[0].content, list)
    assert req.messages[0].content[0]["type"] == "image_url"
