"""Controlled vocabulary for decision buckets — the one place it is spelled.

Extracted here by ROB-1283 so more than one table can carry the vocabulary
without the importers being coupled to each other's mapper graphs. It
previously lived in ``investment_symbol_intermediate_reports``, whose module
also defines a mapper with a ``review.investment_stage_runs`` foreign key;
importing it from ``app.models.review`` to reuse the tuple would have forced
that unrelated mapper (and its FK target) to be configured for every consumer
of the review models.

This module deliberately imports nothing from ``app`` — it is a leaf, so any
table, schema, or service can build a CHECK constraint or a validator from the
same source without dragging a mapper along.

``investment_symbol_intermediate_reports`` re-exports these names, so existing
``from app.models.investment_symbol_intermediate_reports import
DECISION_BUCKETS`` imports keep working unchanged.
"""

from __future__ import annotations

# ``deferred_no_action`` is the negative class: a candidate that was evaluated
# and deliberately not acted on. ROB-1283 records it on ``review.trade_forecasts``
# as well as on report items, so the rejected cohort is queryable rather than
# reconstructable only by regex over prose.
DECISION_BUCKETS: tuple[str, ...] = (
    "new_buy_candidate",
    "open_action",
    "completed_or_existing",
    "deferred_no_action",
    "risk_watch",
)


def sql_in_list(values: tuple[str, ...]) -> str:
    """Render a tuple as a SQL IN-list literal: ``'a', 'b'``.

    Used to build CHECK constraints from the tuple above, so the DB constraint
    and the Python vocabulary cannot drift.
    """
    return ", ".join(f"'{v}'" for v in values)


__all__ = ["DECISION_BUCKETS", "sql_in_list"]
