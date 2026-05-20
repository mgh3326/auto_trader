"""LLM call budget guard for staged reports (ROB-279).

Per-report cap: 4 (3 reducers + 1 composer). Stages that would overshoot
must degrade to deterministic-only fallback or `UNAVAILABLE`."""

from __future__ import annotations

import dataclasses


class BudgetExceeded(Exception):
    pass


@dataclasses.dataclass
class StageLLMBudget:
    max_calls: int = 4
    _used: list[str] = dataclasses.field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(self.max_calls - len(self._used), 0)

    def consume(self, label: str) -> None:
        if len(self._used) >= self.max_calls:
            raise BudgetExceeded(
                f"LLM budget exhausted (cap={self.max_calls}, used={self._used})"
            )
        self._used.append(label)

    def used(self) -> list[str]:
        return list(self._used)
