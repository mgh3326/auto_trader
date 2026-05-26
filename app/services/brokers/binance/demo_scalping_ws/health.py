"""ROB-317 — daemon health/heartbeat snapshot (pure data + JSON).

Emitted for Hermes/Prefect liveness polling. Contains only operational
status — never credentials or order payloads. See ROB-317 design §8.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SymbolHealth:
    """Per-symbol freshness view for the health snapshot."""

    symbol: str
    fresh: bool
    last_event_at: dt.datetime | None
    age_seconds: float | None


@dataclass(frozen=True, slots=True)
class DaemonHealthSnapshot:
    """Point-in-time daemon liveness snapshot."""

    generated_at: dt.datetime
    connected: bool
    daemon_active: bool
    mutation_allowed: bool
    symbols: tuple[SymbolHealth, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "connected": self.connected,
            "daemon_active": self.daemon_active,
            "mutation_allowed": self.mutation_allowed,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "fresh": s.fresh,
                    "last_event_at": (
                        s.last_event_at.isoformat() if s.last_event_at else None
                    ),
                    "age_seconds": s.age_seconds,
                }
                for s in self.symbols
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)
