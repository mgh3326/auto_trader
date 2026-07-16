"""ROB-905 — fail-closed validated-signal gate for Demo scalping confirm=true.

Per ROB-316's documented posture, ``confirm=true`` under any recurring
activation (Prefect / scheduler / TaskIQ / launchd) requires a **separate
validated-signal gate** — the micro-breakout signal is OOS gross-negative and
must stay dry-run/observe-only until a validated-signal artifact says
otherwise. This module enforces that boundary in code so the documented safety
gate can no longer be bypassed by env flags alone.

The gate reads a JSON artifact whose path is supplied by
``BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH`` (default unset). ``confirm=true``
is honoured only when that artifact:

* exists and is a readable file,
* parses as a JSON object,
* declares ``schema == "validated_signal_gate.v1"``,
* carries ``verdict == "validated"``, and
* if it has a ``valid_until`` (ISO-8601), that instant is still in the future.

Every other outcome is fail-closed (``allowed=False``) with a distinct,
machine-readable ``reason``. No exception is ever raised out of this module —
an unexpected failure resolves to "not allowed", never "allowed".

Design constraint: **stdlib only** (no broker / DB / network / pydantic
imports). It runs on the order hot path, so it stays dependency-free.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

#: Env var naming the gate artifact path. Unset → fail-closed (``gate_path_unset``).
_GATE_PATH_ENV = "BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH"

#: Source of truth: ``app.schemas.validated_run_card.GATE_SCHEMA``. Duplicated
#: here as a bare literal to keep this module stdlib-only (importing the schema
#: module would pull pydantic onto the order hot path). A unit test
#: (``test_local_schema_literal_matches_schema_module``) asserts the two never
#: drift apart.
_GATE_SCHEMA = "validated_signal_gate.v1"


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a validated-signal gate evaluation.

    ``allowed`` is the fail-closed verdict; ``reason`` is a stable slug
    (see module docstring) for logging/telemetry; ``evidence`` is a small,
    JSON-safe dict for audit context (never contains secrets).
    """

    allowed: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _deny(reason: str, **evidence: Any) -> GateDecision:
    return GateDecision(allowed=False, reason=reason, evidence=dict(evidence))


def _parse_iso8601(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    # Naive timestamps are interpreted as UTC (conservative, comparable).
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def evaluate_validated_signal_gate(*, now: dt.datetime | None = None) -> GateDecision:
    """Evaluate the validated-signal gate. Fail-closed on every anomaly.

    ``now`` is injectable for deterministic expiry checks; it defaults to the
    current UTC time.
    """
    try:
        raw_path = os.environ.get(_GATE_PATH_ENV)
        if not raw_path or not raw_path.strip():
            return _deny("gate_path_unset")
        path = raw_path.strip()

        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            return _deny("gate_file_missing", path=path)
        except (IsADirectoryError, PermissionError, OSError) as exc:
            return _deny("gate_file_unreadable", path=path, error=type(exc).__name__)

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return _deny("gate_invalid_json", path=path)
        if not isinstance(data, dict):
            return _deny("gate_invalid_json", path=path)

        schema = data.get("schema")
        if schema != _GATE_SCHEMA:
            return _deny("gate_schema_mismatch", schema=schema, expected=_GATE_SCHEMA)

        verdict = data.get("verdict")
        if verdict != "validated":
            return _deny("gate_verdict_not_validated", verdict=verdict)

        valid_until = data.get("valid_until")
        if valid_until is not None:
            parsed = _parse_iso8601(valid_until)
            if parsed is None:
                # Unparseable expiry → cannot prove freshness → fail closed.
                return _deny("gate_expired", valid_until=valid_until)
            now_dt = now or dt.datetime.now(dt.UTC)
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=dt.UTC)
            if parsed <= now_dt:
                return _deny("gate_expired", valid_until=valid_until)

        return GateDecision(
            allowed=True,
            reason="validated",
            evidence={
                "schema": schema,
                "verdict": verdict,
                "valid_until": valid_until,
            },
        )
    except Exception as exc:  # noqa: BLE001 — absolute fail-closed backstop
        return _deny("gate_file_unreadable", error=type(exc).__name__)
