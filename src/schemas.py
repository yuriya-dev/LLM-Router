# src/schemas.py
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


class MessageContent(BaseModel):
    """Represents a single message in the conversation."""
    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, List[Dict[str, Any]]]  # str or multimodal content parts


class ChatCompletionRequest(BaseModel):
    """
    OpenAI-compatible chat completion request schema.
    Validates the incoming body before it reaches the routing logic.
    """
    model: str = Field(..., min_length=1, description="The model to use for completion")
    messages: List[MessageContent] = Field(..., min_length=1)

    # Optional generation parameters
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

    # Allow extra provider-specific fields (e.g. Anthropic's system prompt format)
    model_config = {"extra": "allow"}

    @field_validator("messages")
    @classmethod
    def messages_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("'messages' must contain at least one message")
        return v
