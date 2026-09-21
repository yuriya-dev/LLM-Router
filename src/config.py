# src/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, List


class Settings(BaseSettings):
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""
    PORT: int = 8000

    # ── Authentication ────────────────────────────────────────────────────────
    # Single-tenant mode: one shared Bearer token for all callers.
    ROUTER_API_KEY: Optional[str] = None

    # Multi-tenant mode: each caller has their own key stored in client_keys table.
    # When True, ROUTER_API_KEY is only used for /admin/* endpoints.
    MULTI_TENANT: bool = False

    # ── Encryption ────────────────────────────────────────────────────────────
    # Fernet-compatible 32-byte base64 key for encrypting provider API keys at rest.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # If not set, keys are stored and retrieved as plaintext (backward-compatible).
    ENCRYPTION_KEY: Optional[str] = None

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_RPM: int = 60       # requests per minute per identity (Bearer token / IP)
    RATE_LIMIT_BURST: int = 10     # max burst per second on /v1/chat/completions

    # ── Key Cooldown Durations ────────────────────────────────────────────────
    COOLDOWN_RATE_LIMIT_SECS: int = 60    # on HTTP 429
    COOLDOWN_NETWORK_ERROR_SECS: int = 15  # on 5xx / network errors

    # ── Key Pool Cache ────────────────────────────────────────────────────────
    # In-memory cache TTL for healthy keys per provider.
    # Avoids a Supabase round-trip on every incoming request.
    KEY_CACHE_TTL: int = 30

    # Interval (seconds) for the background task that restores expired cooldown keys.
    COOLDOWN_RESTORE_INTERVAL: int = 30

    # ── Retry / Backoff ───────────────────────────────────────────────────────
    # Retries for transient network errors (httpx.RequestError) per key attempt.
    MAX_RETRIES: int = 2
    RETRY_BACKOFF_BASE: float = 0.5  # seconds; actual delay = base * 2^attempt

    # ── Circuit Breaker ───────────────────────────────────────────────────────
    # After CIRCUIT_BREAKER_FAILURE_THRESHOLD consecutive key failures a provider's
    # circuit opens and requests skip it for CIRCUIT_BREAKER_RECOVERY_SECS seconds.
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_RECOVERY_SECS: float = 30.0

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated allowed origins, or "*" for all.
    # Example: "https://myapp.com,https://admin.myapp.com"
    CORS_ALLOWED_ORIGINS: str = "*"

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def cors_origins(self) -> List[str]:
        if self.CORS_ALLOWED_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


settings = Settings()
