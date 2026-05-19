"""ROB-273 — recursive JSON-safe normalisation for snapshot-backed reports.

The :class:`SnapshotBackedReportGenerator` ingests caller-supplied items and
collector payloads that may contain :class:`~decimal.Decimal`,
:class:`~datetime.datetime`, :class:`~datetime.date`, :class:`~uuid.UUID`, or
:class:`enum.Enum` values. These can't be persisted directly into JSONB
without round-tripping through string/float, so the generator normalises
every JSONB-bound dict/list immediately before building the
:class:`IngestReportRequest`.

The function is deliberately small and pure — no I/O, no logging, no
external dependencies. It is import-safe for both the service layer and
unit tests.

Decimal policy
--------------
Decimals are converted to **strings** by default. The decimal → string
round-trip preserves precision and matches the existing ``WatchCondition``
threshold serialisation contract (``str(Decimal)``). Callers that want
``float`` semantics for a specific field should cast at the call site
rather than relying on a global toggle here.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Return a JSON-safe deep-copy of ``value``.

    Container traversal is recursive: ``dict`` keys are coerced to strings,
    ``list``/``tuple``/``set`` become lists of normalised values, and any
    other value passes through :func:`_atom`.
    """
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return _atom(value)


def _atom(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is a subclass of int — handle before int
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    if isinstance(value, bytes):
        # bytes can't round-trip cleanly to JSON; refuse rather than silently
        # base64-encoding (which would mask a real upstream bug).
        raise TypeError("bytes payloads are not JSONB-safe; encode at the call site")
    if hasattr(value, "model_dump"):
        # Pydantic v2 model — defer to its own JSON-mode dump for parity
        # with how IngestReportRequest serialises nested watch conditions.
        return to_jsonable(value.model_dump(mode="json"))
    raise TypeError(
        f"to_jsonable: unsupported type {type(value).__name__!r}"
    )
