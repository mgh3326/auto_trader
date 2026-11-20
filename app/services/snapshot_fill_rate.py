"""Fill-rate tally for snapshot runners (hk 5752).

A runner's dry-run must say, in one machine-readable line, how many rows it
would write and what share of those rows carries a non-null value for each key
field. The design session reads that line to decide whether a scheduled DAG is
complete enough to register. Pure: no database, no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

FILL_RATE_LINE_PREFIX = "FILL_RATE_JSON "


@dataclass
class FillRateTally:
    """Counts rows and non-null values per key field across batches."""

    fields: tuple[str, ...]
    rows: int = 0
    non_null: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("fill-rate fields must be unique")
        self.non_null = dict.fromkeys(self.fields, 0)

    def add(self, rows: Iterable[Any]) -> None:
        for row in rows:
            self.rows += 1
            for name in self.fields:
                if getattr(row, name, None) is not None:
                    self.non_null[name] += 1

    def summary(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "fields": {
                name: {
                    "non_null": count,
                    "share": round(count / self.rows, 4) if self.rows else None,
                }
                for name, count in self.non_null.items()
            },
        }


def format_fill_rate_line(job: str, summary: dict[str, Any]) -> str:
    """One stdout line the design session can grep and parse as JSON."""
    payload = {"job": job, **summary}
    return FILL_RATE_LINE_PREFIX + json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    )
