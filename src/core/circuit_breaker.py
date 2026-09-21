# src/core/circuit_breaker.py
"""
Per-provider circuit breaker — prevents hammering a failing provider.

State machine:
  CLOSED    → Normal; requests pass through.
  OPEN      → Too many consecutive failures; requests to this provider are
               rejected immediately (fast-fail) without touching Supabase.
  HALF-OPEN → Recovery window; one request is allowed through to test
               whether the provider has recovered. Success → CLOSED;
               failure → OPEN again for another recovery period.

Configuration (via src/config.py / .env):
  CIRCUIT_BREAKER_FAILURE_THRESHOLD  – failures before opening (default 5)
  CIRCUIT_BREAKER_RECOVERY_SECS     – seconds before half-opening (default 30)

Usage (main.py):
    from src.core.circuit_breaker import circuit_breaker

    if circuit_breaker.is_open("groq"):
        continue  # skip this provider in fallback chain

    try:
        response = await execute_request(...)
        circuit_breaker.record_success("groq")
    except ProviderError:
        circuit_breaker.record_failure("groq")
"""
import time
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class _ProviderState:
    failure_count: int = 0
    last_failure_at: float = 0.0
    is_open: bool = False
    open_until: float = 0.0
    total_tripped: int = 0  # lifetime trip count (useful for monitoring)


class CircuitBreaker:
    """Thread-safe, per-provider circuit breaker singleton."""

    def __init__(self, failure_threshold: int = 5, recovery_secs: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_secs = recovery_secs
        self._states: Dict[str, _ProviderState] = {}
        self._lock = Lock()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _state(self, provider: str) -> _ProviderState:
        if provider not in self._states:
            self._states[provider] = _ProviderState()
        return self._states[provider]

    # ── Public API ────────────────────────────────────────────────────────────

    def is_open(self, provider: str) -> bool:
        """
        Returns True if requests to this provider should be skipped.
        Automatically transitions from OPEN → HALF-OPEN when recovery time elapses.
        """
        with self._lock:
            state = self._state(provider)
            if not state.is_open:
                return False
            if time.monotonic() >= state.open_until:
                # Transition to HALF-OPEN: reset failure count, allow one attempt
                state.is_open = False
                state.failure_count = 0
                logger.info(
                    f"[circuit_breaker] provider={provider} → HALF-OPEN (recovery attempt)",
                    extra={"event": "circuit_half_open", "provider": provider}
                )
                return False
            return True

    def record_failure(self, provider: str) -> None:
        """Record a failed attempt; open the circuit if threshold is reached."""
        with self._lock:
            state = self._state(provider)
            state.failure_count += 1
            state.last_failure_at = time.monotonic()
            if state.failure_count >= self.failure_threshold and not state.is_open:
                state.is_open = True
                state.open_until = time.monotonic() + self.recovery_secs
                state.total_tripped += 1
                logger.warning(
                    f"[circuit_breaker] provider={provider} → OPEN "
                    f"after {state.failure_count} failures; "
                    f"recovering in {self.recovery_secs}s",
                    extra={
                        "event": "circuit_opened",
                        "provider": provider,
                        "failure_count": state.failure_count,
                        "recovery_secs": self.recovery_secs,
                        "total_tripped": state.total_tripped,
                    }
                )

    def record_success(self, provider: str) -> None:
        """Record a successful attempt; close the circuit."""
        with self._lock:
            state = self._state(provider)
            if state.is_open or state.failure_count > 0:
                logger.info(
                    f"[circuit_breaker] provider={provider} → CLOSED after success",
                    extra={"event": "circuit_closed", "provider": provider}
                )
            state.failure_count = 0
            state.is_open = False
            state.open_until = 0.0

    def get_status(self) -> Dict[str, dict]:
        """Return a snapshot of all provider circuit states (for /admin/circuit-breakers)."""
        with self._lock:
            return {
                provider: {
                    "state": "OPEN" if state.is_open else "CLOSED",
                    "failure_count": state.failure_count,
                    "open_until": state.open_until if state.is_open else None,
                    "total_tripped": state.total_tripped,
                }
                for provider, state in self._states.items()
            }

    def reset(self, provider: str) -> bool:
        """Manually reset (close) a provider's circuit. Returns True if it existed."""
        with self._lock:
            if provider not in self._states:
                return False
            self._states[provider] = _ProviderState()
            logger.info(
                f"[circuit_breaker] provider={provider} manually reset",
                extra={"event": "circuit_manual_reset", "provider": provider}
            )
            return True


# ── Application-wide singleton (re-configured in main.py lifespan) ─────────
circuit_breaker = CircuitBreaker()
