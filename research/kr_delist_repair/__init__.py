"""KR delisting / terminal-outcome repair and causal daily export (ROB-1430).

Research-only surface.  It reads the immutable ``kr-corpus-v1`` main-scope
snapshot (never the sealed holdout) and public KIND / KIS master pages, and
writes a resolution table plus a causal daily dataset where every row carries
``available_at``.

This package has no imports from ``app`` and performs no database, broker,
scheduler, or LLM access.  Its only network use is the explicit ``collect``
command, which fetches public KIND pages and the public KIS master files.
"""

from __future__ import annotations
