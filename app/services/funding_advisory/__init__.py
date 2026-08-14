"""Advisory-only funding evidence and presentation services.

This package must not feed operator-declared cash into available, required,
shortfall, sizing, caps, eligibility, auto-approval, or broker submission.
Those calculations remain broker-authoritative and are outside this feature.
"""

from .external_cash import ExternalCashDeclarationService

__all__ = ["ExternalCashDeclarationService"]
