# src/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    SUPABASE_URL: str
    SUPABASE_KEY: str
    PORT: int = 8000
    ROUTER_API_KEY: Optional[str] = None

    # Rate limiting (requests per minute per unique key/IP)
    RATE_LIMIT_RPM: int = 60           # /v1/chat/completions limit
    RATE_LIMIT_BURST: int = 10         # max burst per second on that endpoint

    # Key cooldown durations (seconds)
    COOLDOWN_RATE_LIMIT_SECS: int = 60    # on HTTP 429 (rate limited by provider)
    COOLDOWN_NETWORK_ERROR_SECS: int = 15  # on 5xx / network errors

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
