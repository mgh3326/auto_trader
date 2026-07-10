# app/services/order_proposals/service.py  (minimal — fleshed out in Task 6)
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.order_proposals.repository import OrderProposalRepository


class OrderProposalsService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = OrderProposalRepository(session)
        self._session = session
