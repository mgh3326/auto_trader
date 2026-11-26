from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class Evidence:
    value: Any
    source: str | None
    as_of: str | None
    reason: str | None = None
    coverage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "value": self.value,
            "source": self.source,
            "as_of": self.as_of,
            "reason": self.reason,
        }
        if self.coverage is not None:
            result["coverage"] = self.coverage
        return result


@dataclass(frozen=True, slots=True)
class CloseSummary:
    session_date: date
    facts: dict[str, Any]
    interpretation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date.isoformat(),
            "schema_version": 1,
            "facts": self.facts,
            "interpretation": self.interpretation,
        }
