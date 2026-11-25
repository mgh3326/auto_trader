"""Benzinga calendar ingestion into market_events (hk 6513).

Read-only client + global-event normalizers into ``market_events`` with
``source = "benzinga"``. Provider revisions are kept in
``market_event_revisions``; nothing is overwritten silently.

Every entry point is gated: ``BENZINGA_CALENDARS_ENABLED`` must be exactly
``"true"`` and ``BENZINGA_API_KEY`` must hold the key. No database, scheduler or
order path is reachable from the client.
"""

from __future__ import annotations

__all__ = ["client", "compare", "ingestion", "normalizers", "spec"]
