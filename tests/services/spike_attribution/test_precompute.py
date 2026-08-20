"""Phase 2 refresh contract, especially what happens when a refresh fails.

A failed refresh must never look like a successful one. The rules pinned here:
a previous good entry survives with the error stamped on it and its original
``computed_at`` (so it ages into ``stale`` honestly), and a symbol that was
never computed stays ``missing`` rather than getting a synthetic empty entry
that would read as "computed, no catalyst".
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.services.spike_attribution.cache import (
    MODE_INTRADAY,
    MODE_PREOPEN,
    STATE_FRESH,
    STATE_MISSING,
    STATE_STALE,
    CacheEntry,
    lookup,
    read_entry,
    write_entry,
)
from app.services.spike_attribution.precompute import (
    MODES,
    precompute_session,
    refresh_symbol,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
LATER = NOW + dt.timedelta(minutes=10)
SESSION = dt.date(2026, 8, 20)


class _StubDB:
    """Stands in for AsyncSession; precompute only passes it through."""


async def _ok_payload(db, *, market, symbol, session_date):
    return {"symbol": symbol, "spike": True, "summary": {"scored_class": "news"}}


async def _negative_payload(db, *, market, symbol, session_date):
    return {"symbol": symbol, "spike": False, "computed_negative": True}


async def _boom(db, *, market, symbol, session_date):
    raise TimeoutError("upstream said no")


@pytest.fixture
def patched(monkeypatch):
    import app.services.spike_attribution.precompute as mod

    def _use(fn):
        monkeypatch.setattr(mod, "compute_symbol_payload", fn)

    return _use


@pytest.mark.asyncio
async def test_a_successful_refresh_writes_a_fresh_entry(
    tmp_path: Path, patched
) -> None:
    patched(_ok_payload)
    outcome = await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="035420",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=NOW,
        root=tmp_path,
    )
    assert outcome.status == "computed"
    assert outcome.spike is True
    read = lookup(
        market="kr", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    assert read.state == STATE_FRESH
    assert read.entry is not None and read.entry.last_error is None


@pytest.mark.asyncio
async def test_a_computed_negative_is_cached_as_an_answer(
    tmp_path: Path, patched
) -> None:
    patched(_negative_payload)
    outcome = await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="005930",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=NOW,
        root=tmp_path,
    )
    assert outcome.status == "computed"
    assert outcome.spike is False
    read = lookup(
        market="kr", session_date=SESSION, symbol="005930", now=NOW, root=tmp_path
    )
    assert read.state == STATE_FRESH  # not "missing"
    assert read.entry is not None and read.entry.payload["computed_negative"] is True


@pytest.mark.asyncio
async def test_a_failed_refresh_preserves_the_last_good_answer(
    tmp_path: Path, patched
) -> None:
    patched(_ok_payload)
    await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="035420",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=NOW,
        root=tmp_path,
    )
    good = read_entry(market="kr", session_date=SESSION, symbol="035420", root=tmp_path)
    assert good is not None

    patched(_boom)
    outcome = await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="035420",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=LATER,
        root=tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.preserved_previous_entry is True
    assert outcome.error is not None and "TimeoutError" in outcome.error

    after = read_entry(
        market="kr", session_date=SESSION, symbol="035420", root=tmp_path
    )
    assert after is not None
    # The payload and its age are untouched — the entry keeps aging honestly.
    assert after.payload == good.payload
    assert after.computed_at == good.computed_at
    # ...and the failure is visible.
    assert after.last_error is not None and "TimeoutError" in after.last_error
    assert after.last_error_at == LATER
    assert after.last_success_at == NOW


@pytest.mark.asyncio
async def test_a_failed_refresh_does_not_reset_the_clock_to_look_fresh(
    tmp_path: Path, patched
) -> None:
    patched(_ok_payload)
    stale_time = NOW - dt.timedelta(hours=3)
    await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="035420",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=stale_time,
        root=tmp_path,
    )
    patched(_boom)
    await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="035420",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=NOW,
        root=tmp_path,
    )
    read = lookup(
        market="kr", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    # The failure must not have laundered a 3-hour-old answer into a fresh one.
    assert read.state == STATE_STALE
    assert "the last refresh FAILED" in " ".join(read.notes)


@pytest.mark.asyncio
async def test_a_never_computed_symbol_stays_missing_after_a_failure(
    tmp_path: Path, patched
) -> None:
    # The dangerous alternative would be writing an empty entry, which reads as
    # "computed, nothing found" — exactly the confusion this design forbids.
    patched(_boom)
    outcome = await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="000660",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=NOW,
        root=tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.preserved_previous_entry is False
    read = lookup(
        market="kr", session_date=SESSION, symbol="000660", now=NOW, root=tmp_path
    )
    assert read.state == STATE_MISSING
    assert read.entry is None
    assert not list(tmp_path.rglob("000660.json"))


# --- run-level reporting -------------------------------------------------


@pytest.mark.asyncio
async def test_a_partial_run_reports_partial_not_ok(
    tmp_path: Path, monkeypatch
) -> None:
    import app.services.spike_attribution.precompute as mod

    async def _mixed(db, *, market, symbol, session_date):
        if symbol == "BAD":
            raise RuntimeError("nope")
        return {"symbol": symbol, "spike": False}

    monkeypatch.setattr(mod, "compute_symbol_payload", _mixed)
    run = await precompute_session(
        _StubDB(),
        market="kr",
        session_date=SESSION,
        symbols=["035420", "BAD", "035720"],
        mode=MODE_PREOPEN,
        now=NOW,
        root=tmp_path,
    )
    payload = run.as_dict()
    assert payload["run_status"] == "partial"
    assert payload["counts"] == {"attempted": 3, "succeeded": 2, "failed": 1}
    assert payload["db_rows_written"] == 0
    assert payload["scheduler_registration"] is False


@pytest.mark.asyncio
async def test_a_clean_run_reports_ok(tmp_path: Path, patched) -> None:
    patched(_ok_payload)
    run = await precompute_session(
        _StubDB(),
        market="kr",
        session_date=SESSION,
        symbols=["035420", "035720"],
        mode=MODE_PREOPEN,
        now=NOW,
        root=tmp_path,
    )
    assert run.as_dict()["run_status"] == "ok"
    assert run.failed == 0


@pytest.mark.asyncio
async def test_one_symbol_failing_does_not_abort_the_others(
    tmp_path: Path, monkeypatch
) -> None:
    import app.services.spike_attribution.precompute as mod

    async def _first_fails(db, *, market, symbol, session_date):
        if symbol == "035420":
            raise RuntimeError("nope")
        return {"symbol": symbol, "spike": True, "summary": {"scored_class": "news"}}

    monkeypatch.setattr(mod, "compute_symbol_payload", _first_fails)
    run = await precompute_session(
        _StubDB(),
        market="kr",
        session_date=SESSION,
        symbols=["035420", "035720"],
        mode=MODE_INTRADAY,
        now=NOW,
        root=tmp_path,
    )
    assert run.succeeded == 1 and run.failed == 1
    survivor = lookup(
        market="kr", session_date=SESSION, symbol="035720", now=NOW, root=tmp_path
    )
    assert survivor.state == STATE_FRESH


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await precompute_session(
            _StubDB(),
            market="kr",
            session_date=SESSION,
            symbols=["035420"],
            mode="whenever",
            now=NOW,
            root=tmp_path,
        )


def test_modes_are_exactly_the_pinned_two() -> None:
    assert set(MODES) == {MODE_PREOPEN, MODE_INTRADAY}


@pytest.mark.asyncio
async def test_an_intraday_refresh_overwrites_a_preopen_entry_cleanly(
    tmp_path: Path, patched
) -> None:
    write_entry(
        CacheEntry(
            market="kr",
            session_date=SESSION,
            symbol="035420",
            mode=MODE_PREOPEN,
            computed_at=NOW - dt.timedelta(hours=2),
            spec_sha256="old",
            payload={"symbol": "035420", "spike": False},
        ),
        root=tmp_path,
    )
    patched(_ok_payload)
    await refresh_symbol(
        _StubDB(),
        market="kr",
        symbol="035420",
        session_date=SESSION,
        mode=MODE_INTRADAY,
        now=NOW,
        root=tmp_path,
    )
    after = read_entry(
        market="kr", session_date=SESSION, symbol="035420", root=tmp_path
    )
    assert after is not None
    assert after.mode == MODE_INTRADAY
    assert after.payload["spike"] is True
    assert after.computed_at == NOW
