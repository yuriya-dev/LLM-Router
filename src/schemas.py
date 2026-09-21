# src/schemas.py
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


class MessageContent(BaseModel):
    """Represents a single message in the conversation (OpenAI-compatible)."""
    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, List[Dict[str, Any]]]  # str or multimodal content parts


class ChatCompletionRequest(BaseModel):
    """
    OpenAI-compatible chat completion request schema.
    Extended with optional router-specific fields:
      - metadata : arbitrary key-value pairs logged to request_logs for audit/billing
      - timeout  : per-request timeout in seconds (overrides provider default)
    """
    model: str = Field(..., min_length=1, description="Model to use for completion")
    messages: List[MessageContent] = Field(..., min_length=1)

    # Standard OpenAI generation parameters
    stream: bool = False
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    presence_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    stop: Optional[Union[str, List[str]]] = None
    n: Optional[int] = Field(default=None, ge=1, le=10)
    user: Optional[str] = None
    stream_options: Optional[Dict[str, Any]] = None

    # ── Router-specific extensions ────────────────────────────────────────────
    # Arbitrary metadata stored in request_logs (e.g. user_id, session_id, app_name).
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional key-value metadata logged with the request for audit/billing"
    )
    # Per-request timeout in seconds. Overrides the provider's default timeout.
    timeout: Optional[float] = Field(
        default=None, gt=0,
        description="Per-request timeout in seconds (overrides provider default)"
    )

    # Allow extra provider-specific fields (e.g. Anthropic top_k, thinking budget)
    model_config = {"extra": "allow"}

    @field_validator("messages")
    @classmethod
    def messages_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("'messages' must contain at least one message")
        return v

    def has_image_content(self) -> bool:
        """Return True if any message contains an image_url content part."""
        for msg in self.messages:
            if isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False
