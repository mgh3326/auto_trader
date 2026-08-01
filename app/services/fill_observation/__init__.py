"""Broker-evidenced immutable fill observation foundation (ROB-1195)."""

from app.services.fill_observation.contracts import (
    BrokerFillEvidence,
    FillDualReadStatus,
    FillDualReadValidation,
    FillObservationWriteResult,
    FillObservationWriteStatus,
    FillProjectionDelivery,
)
from app.services.fill_observation.dual_read import FillObservationDualReader
from app.services.fill_observation.errors import (
    FillObservationError,
    FillObservationIdentityConflict,
    FillProjectionCursorRegression,
    FillProjectionDeliveryError,
    FillProjectionLeaseMismatch,
    InvalidFillEvidence,
    NonMonotonicFillCumulative,
)
from app.services.fill_observation.projection import FillProjectionQueue
from app.services.fill_observation.writer import (
    DEFAULT_FILL_PROJECTIONS,
    FILL_OBSERVATION_WRITER_ENABLED_ENV,
    FillObservationWriter,
    fill_observation_writer_enabled,
)

__all__ = [
    "DEFAULT_FILL_PROJECTIONS",
    "FILL_OBSERVATION_WRITER_ENABLED_ENV",
    "BrokerFillEvidence",
    "FillDualReadStatus",
    "FillDualReadValidation",
    "FillObservationDualReader",
    "FillObservationError",
    "FillObservationIdentityConflict",
    "FillObservationWriteResult",
    "FillObservationWriteStatus",
    "FillObservationWriter",
    "FillProjectionDelivery",
    "FillProjectionCursorRegression",
    "FillProjectionDeliveryError",
    "FillProjectionLeaseMismatch",
    "FillProjectionQueue",
    "InvalidFillEvidence",
    "NonMonotonicFillCumulative",
    "fill_observation_writer_enabled",
]
