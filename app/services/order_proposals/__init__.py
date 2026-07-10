"""order_proposals package.

Exports OrderProposalsService lazily (PEP 562 module __getattr__) to avoid a
circular import: app.models.order_proposals imports
app.services.order_proposals.state_machine at module level, which triggers
this package's __init__ before the model classes are defined. Eagerly
importing service.py here (which pulls in repository.py -> app.models.order_proposals)
would re-enter the partially-initialized models module. Deferring the import
until OrderProposalsService is actually accessed sidesteps the cycle while
keeping the same public import surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.order_proposals.service import OrderProposalsService

__all__ = ["OrderProposalsService"]


def __getattr__(name: str):
    if name == "OrderProposalsService":
        from app.services.order_proposals.service import OrderProposalsService

        return OrderProposalsService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
