"""Phase 2 cache contract: three states, and a miss that cannot pass as an answer.

The hazard this pins down: a session reads the cache first, so an empty or
stale answer silently becomes "no catalyst" — which is a different claim from
`unattributed`. These tests fix the boundary between the three states and the
rule that a computed negative is *fresh*, not *missing*.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.services.spike_attribution.cache import (
    EXPECTED_REFRESH_SECONDS_BY_MODE,
    GRACE_SECONDS,
    MODE_INTRADAY,
    MODE_PREOPEN,
    STATE_FRESH,
    STATE_MISSING,
    STATE_STALE,
    CacheEntry,
    CacheError,
    classify_state,
    lookup,
    read_entry,
    write_entry,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
SESSION = dt.date(2026, 8, 20)


def entry(
    *,
    computed_at: dt.datetime,
    mode: str = MODE_INTRADAY,
    payload: dict | None = None,
    last_error: str | None = None,
    last_error_at: dt.datetime | None = None,
    last_success_at: dt.datetime | None = None,
) -> CacheEntry:
    return CacheEntry(
        market="kr",
        session_date=SESSION,
        symbol="035420",
        mode=mode,
        computed_at=computed_at,
        spec_sha256="deadbeef",
        payload=payload if payload is not None else {"symbol": "035420", "spike": True},
        last_success_at=last_success_at,
        last_error=last_error,
        last_error_at=last_error_at,
    )


# --- state boundaries ----------------------------------------------------


def test_no_entry_is_missing() -> None:
    state, age, expected = classify_state(entry=None, now=NOW)
    assert state == STATE_MISSING
    assert age is None and expected is None


@pytest.mark.parametrize("mode", [MODE_PREOPEN, MODE_INTRADAY])
def test_just_computed_is_fresh(mode: str) -> None:
    state, age, expected = classify_state(
        entry=entry(computed_at=NOW, mode=mode), now=NOW
    )
    assert state == STATE_FRESH
    assert age == 0
    assert expected == EXPECTED_REFRESH_SECONDS_BY_MODE[mode]


def test_fresh_right_up_to_cadence_plus_grace() -> None:
    cadence = EXPECTED_REFRESH_SECONDS_BY_MODE[MODE_INTRADAY]
    at_edge = NOW - dt.timedelta(seconds=cadence + GRACE_SECONDS)
    state, _, _ = classify_state(entry=entry(computed_at=at_edge), now=NOW)
    assert state == STATE_FRESH


def test_stale_one_second_past_the_edge() -> None:
    cadence = EXPECTED_REFRESH_SECONDS_BY_MODE[MODE_INTRADAY]
    past = NOW - dt.timedelta(seconds=cadence + GRACE_SECONDS + 1)
    state, age, _ = classify_state(entry=entry(computed_at=past), now=NOW)
    assert state == STATE_STALE
    assert age is not None and age > cadence


def test_preopen_entries_tolerate_a_much_longer_age_than_intraday() -> None:
    # A pre-open precompute is meant to last the morning; the intraday cadence
    # is 15 minutes. The same age must not be judged by the same yardstick.
    age = dt.timedelta(hours=2)
    assert (
        classify_state(entry=entry(computed_at=NOW - age, mode=MODE_PREOPEN), now=NOW)[
            0
        ]
        == STATE_FRESH
    )
    assert (
        classify_state(entry=entry(computed_at=NOW - age, mode=MODE_INTRADAY), now=NOW)[
            0
        ]
        == STATE_STALE
    )


# --- the load-bearing distinction ---------------------------------------


def test_a_computed_negative_is_fresh_not_missing(tmp_path: Path) -> None:
    """ "We looked and there was no spike" is an answer, not an absence."""

    write_entry(
        entry(computed_at=NOW, payload={"symbol": "035420", "spike": False}),
        root=tmp_path,
    )
    read = lookup(
        market="kr", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    assert read.state == STATE_FRESH
    assert read.usable_without_fallback is True
    assert read.requires_live_fallback is False
    assert read.entry is not None and read.entry.payload["spike"] is False


def test_a_cached_unattributed_is_fresh_and_stays_unattributed(tmp_path: Path) -> None:
    payload = {
        "symbol": "035420",
        "spike": True,
        "summary": {"unattributed": True, "scored_class": "unattributed"},
    }
    write_entry(entry(computed_at=NOW, payload=payload), root=tmp_path)
    read = lookup(
        market="kr", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    assert read.state == STATE_FRESH
    assert read.entry is not None
    assert read.entry.payload["summary"]["unattributed"] is True


def test_a_miss_says_so_and_refuses_to_imply_no_catalyst(tmp_path: Path) -> None:
    read = lookup(
        market="kr", session_date=SESSION, symbol="NOPE", now=NOW, root=tmp_path
    )
    assert read.state == STATE_MISSING
    assert read.requires_live_fallback is True
    assert read.usable_without_fallback is False
    assert read.entry is None
    payload = read.as_dict()
    assert payload["payload"] is None
    assert payload["missing_is_not_no_catalyst"] is True
    assert "NOT evidence that the symbol had no catalyst" in " ".join(payload["notes"])
    assert "fall back to live computation" in payload["notes"]


def test_stale_returns_the_payload_with_its_age(tmp_path: Path) -> None:
    old = NOW - dt.timedelta(hours=3)
    write_entry(entry(computed_at=old), root=tmp_path)
    read = lookup(
        market="kr", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    assert read.state == STATE_STALE
    assert read.entry is not None  # payload is not withheld
    assert read.age_seconds is not None and read.age_seconds == pytest.approx(10800)
    assert read.reason is not None and "past its" in read.reason
    assert read.usable_without_fallback is False


# --- failure path --------------------------------------------------------


def test_a_recorded_failure_is_visible_on_a_stale_entry(tmp_path: Path) -> None:
    old = NOW - dt.timedelta(hours=3)
    write_entry(
        entry(
            computed_at=old,
            last_success_at=old,
            last_error="TimeoutError: upstream",
            last_error_at=NOW - dt.timedelta(minutes=1),
        ),
        root=tmp_path,
    )
    read = lookup(
        market="kr", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    payload = read.as_dict()
    assert read.state == STATE_STALE
    assert payload["last_error"] == "TimeoutError: upstream"
    assert payload["last_success_at"] == old.isoformat()
    assert "the last refresh FAILED" in " ".join(payload["notes"])


def test_a_corrupt_entry_is_missing_with_a_reason_not_a_silent_hit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kr" / SESSION.isoformat() / "035420.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    read = lookup(
        market="kr", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    assert read.state == STATE_MISSING
    assert read.reason is not None and read.reason.startswith("unreadable_entry")
    assert read.requires_live_fallback is True
    with pytest.raises(CacheError):
        read_entry(market="kr", session_date=SESSION, symbol="035420", root=tmp_path)


# --- storage -------------------------------------------------------------


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    original = entry(
        computed_at=NOW,
        last_success_at=NOW,
        last_error="x",
        last_error_at=NOW,
    )
    write_entry(original, root=tmp_path)
    loaded = read_entry(
        market="kr", session_date=SESSION, symbol="035420", root=tmp_path
    )
    assert loaded == original


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    write_entry(entry(computed_at=NOW), root=tmp_path)
    assert not list(tmp_path.rglob("*.tmp"))
    stored = json.loads(
        (tmp_path / "kr" / SESSION.isoformat() / "035420.json").read_text("utf-8")
    )
    assert stored["symbol"] == "035420"


def test_entries_are_partitioned_by_market_and_session(tmp_path: Path) -> None:
    write_entry(entry(computed_at=NOW), root=tmp_path)
    other_day = lookup(
        market="kr",
        session_date=dt.date(2026, 8, 19),
        symbol="035420",
        now=NOW,
        root=tmp_path,
    )
    other_market = lookup(
        market="us", session_date=SESSION, symbol="035420", now=NOW, root=tmp_path
    )
    assert other_day.state == STATE_MISSING
    assert other_market.state == STATE_MISSING


def test_a_symbol_with_a_slash_cannot_escape_the_cache_root(tmp_path: Path) -> None:
    write_entry(
        CacheEntry(
            market="us",
            session_date=SESSION,
            symbol="BRK/B",
            mode=MODE_INTRADAY,
            computed_at=NOW,
            spec_sha256="x",
            payload={},
        ),
        root=tmp_path,
    )
    written = list(tmp_path.rglob("*.json"))
    assert len(written) == 1
    assert tmp_path in written[0].parents
    assert written[0].name == "BRK_B.json"
