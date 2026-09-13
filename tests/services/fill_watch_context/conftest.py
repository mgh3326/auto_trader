"""Producer-shaped artifacts for context-only consumption tests.

These fixtures deliberately contain transport and supplied-context fields only.
They never predeclare an implementation status, reason, or next action.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

UTC = dt.UTC
AS_OF = dt.datetime(2026, 9, 7, 0, 0, tzinfo=UTC)


def event_uuid(index: int) -> str:
    return f"{index:08d}-0000-4000-8000-000000000000"


def context_artifact(
    index: int,
    *,
    event_kind: str = "fill",
    economic_root_ref: str = "root:order-1",
    order_refs: list[str] | None = None,
    market_freshness: str = "fresh",
    position_freshness: str = "fresh",
    route_availability: str = "available",
    artifact_health: str = "complete",
    close_condition: bool = False,
    sample_origin: str = "synthetic",
) -> dict[str, Any]:
    """A raw shape that a producer-side UUID artifact adapter would deliver."""
    return {
        "lane_event": {
            "kind": "lane.event",
            "lane": f"context-{event_kind}",
            "event_id": event_uuid(index),
            "text": f"{event_kind} context event {index}",
            "label": f"{event_kind}-handoff",
            "host": "producer-fixture",
            "pane": "w0:p0",
        },
        "context": {
            "event_kind": event_kind,
            "input_as_of": AS_OF.isoformat(),
            "economic_root_ref": economic_root_ref,
            "order_refs": order_refs or ["order-ref-1"],
            "market_snapshot": {
                "as_of": AS_OF.isoformat(),
                "freshness": market_freshness,
            },
            "position_snapshot": {
                "as_of": AS_OF.isoformat(),
                "freshness": position_freshness,
            },
            "route": {"availability": route_availability},
            "artifact_health": artifact_health,
            "watch": {"close_condition": close_condition}
            if event_kind == "watch"
            else None,
            "sample_origin": sample_origin,
        },
    }
