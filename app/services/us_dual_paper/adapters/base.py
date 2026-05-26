from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas.us_dual_paper import (
    AccountStateSummary,
    BrokerPreviewRequest,
    BrokerPreviewResult,
)


class BrokerUnsupportedError(Exception):
    """Raised when a broker cannot be previewed (e.g. creds/flag missing)."""


class BrokerPreviewAdapter(ABC):
    """Read-only, side-effect-free per-broker preview adapter.

    Implementations MUST NOT import or reach any submit/cancel/modify/place path.
    """

    account_scope: str

    @abstractmethod
    def is_enabled(self) -> bool:
        """True when creds/flags allow read-only access to this broker."""

    @abstractmethod
    def missing_env_keys(self) -> list[str]:
        """Names (never values) of env keys required but absent."""

    @abstractmethod
    async def read_account_state(self) -> AccountStateSummary:
        """Read-only cash/buying-power/positions summary."""

    @abstractmethod
    async def preview(self, req: BrokerPreviewRequest) -> BrokerPreviewResult:
        """Pure validation of a buy/limit order. Never submits."""
