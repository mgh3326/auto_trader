from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.paper_validation.locking import lock_validation_streams


class _RecordingSession:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def execute(self, statement: object, params: dict[str, str]) -> None:
        del statement
        self.keys.append(params["key"])


@pytest.mark.asyncio
async def test_validation_stream_locks_are_sorted_and_deduplicated() -> None:
    session = _RecordingSession()

    await lock_validation_streams(
        cast(AsyncSession, cast(Any, session)),
        ("validation-z", "validation-a", "validation-z", "validation-m"),
    )

    assert session.keys == ["validation-a", "validation-m", "validation-z"]
