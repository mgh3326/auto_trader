"""Bounded official-event backfill requests (hk 6409, design 6400 §3.2).

``policy`` is the pure closed vocabulary and the producer command builder,
``store`` is the only writer of ``review.market_event_backfill_requests``, and
``service`` orders the writes. Only the manual CLI
``scripts.request_market_event_backfill`` calls this package: no router, MCP
tool, retro/v1 reader, task or scheduler may wake a collection.
"""
