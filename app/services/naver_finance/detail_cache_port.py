"""DetailCachePort — the DB-agnostic contract investor.py depends on (ROB-811).

Typing only; imports no DB/session code so the pure scrape module stays free of
runtime DB coupling.
"""

from __future__ import annotations

from typing import Any, Protocol


class DetailCachePort(Protocol):
    async def get_many(self, nids: list[str]) -> dict[str, dict[str, Any]]: ...

    async def put_many(self, entries: dict[str, dict[str, Any]]) -> None: ...