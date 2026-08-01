"""Typed failures for fill observation persistence and projection delivery."""


class FillObservationError(RuntimeError):
    """Base error for the fill-observation service boundary."""


class InvalidFillEvidence(FillObservationError):
    """The supplied facts cannot prove a positive broker fill."""


class FillObservationIdentityConflict(FillObservationError):
    """A deterministic identity was reused with different semantic evidence."""


class NonMonotonicFillCumulative(FillObservationError):
    """Cumulative broker quantity regressed below already recorded deltas."""


class FillProjectionDeliveryError(FillObservationError):
    """Base error for outbox claim/completion operations."""


class FillProjectionLeaseMismatch(FillProjectionDeliveryError):
    """The requested completion/retry does not own the active delivery lease."""


class FillProjectionCursorRegression(FillProjectionDeliveryError):
    """A delivery attempted to move a projection cursor backwards."""


__all__ = [
    "FillObservationError",
    "FillObservationIdentityConflict",
    "FillProjectionCursorRegression",
    "FillProjectionDeliveryError",
    "FillProjectionLeaseMismatch",
    "InvalidFillEvidence",
    "NonMonotonicFillCumulative",
]
