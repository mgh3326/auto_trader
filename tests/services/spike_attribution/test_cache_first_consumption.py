"""The consumption contract: cache-first, but a cold cache is never an answer.

Canon (ROB-1303 body) says the session reads the cache first and falls back to a
live read on miss/stale, with the age exposed. The brief adds the prohibition:
never a silent empty response. These tests fix both halves — a fresh entry
short-circuits the live path entirely, and every other state still reaches the
live path while carrying its cache state.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.services.spike_attribution.cache import (
    MODE_INTRADAY,
    STATE_FRESH,
    STATE_MISSING,
    STATE_STALE,
    CacheEntry,
    write_entry,
)

SESSION = dt.date(2026, 8, 20)


class _NoDB:
    """Any DB use at all fails the test."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __call__(self):
        return self

    async def execute(self, *a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("cache-first path must not touch the database")


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPIKE_ATTRIBUTION_CACHE_DIR", str(tmp_path))
    return tmp_path


def seed(root: Path, *, symbol: str, computed_at: dt.datetime, payload: dict) -> None:
    write_entry(
        CacheEntry(
            market="kr",
            session_date=SESSION,
            symbol=symbol,
            mode=MODE_INTRADAY,
            computed_at=computed_at,
            spec_sha256="pinned",
            payload=payload,
        ),
        root=root,
    )


@pytest.mark.asyncio
async def test_a_fresh_entry_is_served_without_touching_the_database(
    isolated_cache: Path, monkeypatch
) -> None:
    import app.mcp_server.tooling.spike_attribution as mod

    seed(
        isolated_cache,
        symbol="035420",
        computed_at=dt.datetime.now(dt.UTC),
        payload={
            "symbol": "035420",
            "spike": True,
            "summary": {"unattributed": False, "scored_class": "news"},
        },
    )
    monkeypatch.setattr(mod, "_session_factory", lambda: _NoDB())

    res = await mod.get_spike_attribution_impl(
        ["035420"], session_date="2026-08-20", market="kr", created_by="test"
    )
    assert res["success"] is True
    row = res["results"][0]
    assert row["served_from"] == "cache"
    assert row["cache"]["state"] == STATE_FRESH
    assert res["counts"]["served_from_cache"] == 1
    assert res["counts"]["served_live"] == 0


@pytest.mark.asyncio
async def test_a_cached_unattributed_is_served_as_unattributed_not_as_no_catalyst(
    isolated_cache: Path, monkeypatch
) -> None:
    import app.mcp_server.tooling.spike_attribution as mod

    seed(
        isolated_cache,
        symbol="035720",
        computed_at=dt.datetime.now(dt.UTC),
        payload={
            "symbol": "035720",
            "spike": True,
            "summary": {"unattributed": True, "scored_class": "unattributed"},
        },
    )
    monkeypatch.setattr(mod, "_session_factory", lambda: _NoDB())

    res = await mod.get_spike_attribution_impl(
        ["035720"], session_date="2026-08-20", market="kr", created_by="test"
    )
    row = res["results"][0]
    assert row["summary"]["unattributed"] is True
    assert res["counts"]["unattributed"] == 1
    assert row["cache"]["state"] == STATE_FRESH


@pytest.mark.parametrize(
    ("label", "age", "expected_state"),
    [
        ("missing", None, STATE_MISSING),
        ("stale", dt.timedelta(hours=3), STATE_STALE),
    ],
)
@pytest.mark.asyncio
async def test_non_fresh_states_fall_through_to_live_and_say_why(
    isolated_cache: Path,
    monkeypatch,
    label: str,
    age: dt.timedelta | None,
    expected_state: str,
) -> None:
    import app.mcp_server.tooling.spike_attribution as mod

    if age is not None:
        seed(
            isolated_cache,
            symbol="035420",
            computed_at=dt.datetime.now(dt.UTC) - age,
            payload={"symbol": "035420", "spike": True},
        )

    reached_live = {"yes": False}

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _factory():
        return _FakeDB

    async def _fake_bars(db, **kwargs):
        reached_live["yes"] = True
        return []

    monkeypatch.setattr(mod, "_session_factory", _factory)
    monkeypatch.setattr(mod, "load_daily_bars", _fake_bars)

    res = await mod.get_spike_attribution_impl(
        ["035420"], session_date="2026-08-20", market="kr", created_by="test"
    )
    row = res["results"][0]
    assert reached_live["yes"] is True, f"{label} must fall back to a live read"
    assert row["served_from"] == "live"
    assert row["cache"]["state"] == expected_state
    assert row["cache"]["reason"]
    assert res["counts"]["served_live"] == 1


@pytest.mark.asyncio
async def test_a_missing_entry_never_reads_as_absent_catalyst(
    isolated_cache: Path, monkeypatch
) -> None:
    import app.mcp_server.tooling.spike_attribution as mod

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mod, "_session_factory", lambda: _FakeDB)
    monkeypatch.setattr(mod, "load_daily_bars", lambda db, **k: _empty())

    async def _empty():
        return []

    res = await mod.get_spike_attribution_impl(
        ["NOPE"], session_date="2026-08-20", market="kr", created_by="test"
    )
    cache_block = res["results"][0]["cache"]
    assert cache_block["state"] == STATE_MISSING
    assert cache_block["missing_is_not_no_catalyst"] is True
    assert cache_block["payload"] is None
    assert "NOT evidence that the symbol had no catalyst" in " ".join(
        cache_block["notes"]
    )
    assert res["cache_first"] is True
    assert "NOT evidence" in res["cache_state_note"]


@pytest.mark.asyncio
async def test_cache_can_be_bypassed_explicitly(
    isolated_cache: Path, monkeypatch
) -> None:
    import app.mcp_server.tooling.spike_attribution as mod

    seed(
        isolated_cache,
        symbol="035420",
        computed_at=dt.datetime.now(dt.UTC),
        payload={"symbol": "035420", "spike": True},
    )

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _fake_bars(db, **kwargs):
        return []

    monkeypatch.setattr(mod, "_session_factory", lambda: _FakeDB)
    monkeypatch.setattr(mod, "load_daily_bars", _fake_bars)

    res = await mod.get_spike_attribution_impl(
        ["035420"],
        session_date="2026-08-20",
        market="kr",
        created_by="test",
        use_cache=False,
    )
    assert res["cache_first"] is False
    assert res["results"][0]["served_from"] == "live"
    assert res["results"][0]["cache"] is None
