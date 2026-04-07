"""AI Provider base types."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class AiProviderResult(BaseModel):
    """Result from an AI provider call."""

    answer: str
    provider: str
    model: str
    usage: dict[str, Any] | None
    elapsed_ms: int


class AiProviderError(Exception):
    """Provider call error with user-facing message and internal detail."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        self.user_message = user_message
        self.detail = detail
        super().__init__(user_message)


class AiProvider(Protocol):
    """Protocol for AI provider adapters."""

    provider_name: str
    default_model: str

    async def ask(
        self,
        system_prompt: str,
        user_message: str,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> AiProviderResult: ...
