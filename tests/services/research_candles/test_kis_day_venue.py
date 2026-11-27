"""hk 7037: KIS 주간거래 (day-market) bars under venue ``KIS_DAY``.

Pure and fixture-driven (no database): covers the session label of a KIS_DAY bar
(US_DAY inside the Toss calendar dayMarket window, UNKNOWN otherwise), the ingest
decision (accepted with a calendar, refused without one, raw BAY/BAQ/BAA refused
with a closed reason), and the additive venue migration shape. The DB CHECK and
downgrade guard live in ``test_kis_day_venue_db.py`` (CI only).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.brokers.toss.market_calendar import parse_us_market_calendar
from app.services.research_candles import minute_ingest as mi
from app.services.research_candles.session_segment import (
    KIS_DAY_VENUE,
    UNKNOWN,
    US_DAY,
    US_PRE,
    session_segment_of,
    us_calendar_segment,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20261127_hk7037_us_kis_day.py"
MIGRATION_VAL = REPO / "alembic/versions/20261127_hk7037_us_kis_day_val.py"

DATE = "2026-11-25"
NOW = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)


def _window(start: str, end: str) -> dict:
    return {
        "startTime": f"{DATE}T{start}:00+09:00",
        "endTime": f"{DATE}T{end}:00+09:00",
    }


def _calendar(*, pre: tuple[str, str] | None = None):
    """Toss US calendar of one listed day: dayMarket 10:00-17:50 KST."""
    raw = {
        "today": {
            "date": DATE,
            "dayMarket": _window("10:00", "17:50"),
            "preMarket": _window(*pre) if pre else None,
            "regularMarket": None,
            "afterMarket": None,
        }
    }
    return parse_us_market_calendar(raw)


def _start_kst(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{DATE}T{hhmm}:00+09:00")


def _decide(**overrides: Any) -> mi.BarDecision:
    kwargs: dict[str, Any] = {
        "market": "us",
        "symbol": "SPY",
        "venue": KIS_DAY_VENUE,
        "time_utc": _start_kst("11:00"),
        "open_price": Decimal("600.10"),
        "high_price": Decimal("600.60"),
        "low_price": Decimal("600.00"),
        "close_price": Decimal("600.40"),
        "volume": Decimal("100"),
        "value": Decimal("60040"),
        "received_at": _start_kst("11:01:15"),
        "source": "KIS",
        "now": NOW,
        "us_calendar": _calendar(),
    }
    kwargs.update(overrides)
    return mi.decide_bar(**kwargs)


# ---------------------------------------------------------------- session label ----


def test_kis_day_bar_inside_the_day_market_window_is_us_day() -> None:
    assert (
        session_segment_of(
            "us", KIS_DAY_VENUE, _start_kst("11:00"), us_calendar=_calendar()
        )
        == US_DAY
    )


def test_kis_day_bar_outside_the_day_market_window_is_unknown_not_a_kis_window() -> (
    None
):
    # Inside the Toss preMarket window, but KIS_DAY is not a listing venue, so the
    # KIS pre-market window does not apply: UNKNOWN, never a guess.
    calendar = _calendar(pre=("08:00", "09:30"))
    assert us_calendar_segment(_start_kst("08:30").astimezone(UTC), calendar) == US_PRE
    assert (
        session_segment_of(
            "us", KIS_DAY_VENUE, _start_kst("08:30"), us_calendar=calendar
        )
        == UNKNOWN
    )
    assert (
        session_segment_of(
            "us", KIS_DAY_VENUE, _start_kst("18:00"), us_calendar=_calendar()
        )
        == UNKNOWN
    )


def test_kis_day_bar_without_a_calendar_is_unknown() -> None:
    assert session_segment_of("us", KIS_DAY_VENUE, _start_kst("11:00")) == UNKNOWN


# ------------------------------------------------------------------- ingest ----


def test_kis_day_bar_with_calendar_is_stored_as_us_day_under_its_own_venue() -> None:
    decision = _decide()
    assert decision.reason is None
    bar = decision.bar
    assert bar is not None
    assert (bar.venue, bar.session_segment, bar.source) == (
        KIS_DAY_VENUE,
        US_DAY,
        "KIS",
    )
    assert bar.value == Decimal("60040")
    assert bar.symbol == "SPY"
    # First receipt is the claimed receipt, untouched by a later replay.
    assert decision.retrieved_at == _start_kst("11:01:15").astimezone(UTC)


def test_kis_day_bar_without_a_calendar_is_refused_not_labelled_unknown() -> None:
    decision = _decide(us_calendar=None)
    assert decision.bar is None
    assert decision.reason == mi.REASON_CALENDAR_UNAVAILABLE


@pytest.mark.parametrize("raw_code", ["BAY", "BAQ", "BAA", "bay", " BAQ "])
def test_raw_kis_day_exchange_codes_are_refused_with_a_closed_reason(
    raw_code: str,
) -> None:
    decision = _decide(venue=raw_code)
    assert decision.bar is None
    assert decision.reason == mi.REASON_UNKNOWN_VENUE
    assert decision.reason in mi.REASONS


def test_calendar_refusal_is_in_the_closed_vocabulary() -> None:
    assert mi.REASON_CALENDAR_UNAVAILABLE in mi.REASONS
    assert mi.REASON_CALENDAR_UNAVAILABLE == "calendar_unavailable"


def test_kis_day_venue_is_only_admitted_for_us() -> None:
    assert KIS_DAY_VENUE in mi.VENUES_BY_MARKET["us"]
    assert KIS_DAY_VENUE not in mi.VENUES_BY_MARKET["kr"]
    assert KIS_DAY_VENUE not in mi.VENUES_BY_MARKET["futures"]
    decision = _decide(market="kr", symbol="005930", venue=KIS_DAY_VENUE)
    assert decision.reason == mi.REASON_UNKNOWN_VENUE


def test_toss_venue_behaviour_without_a_calendar_is_unchanged() -> None:
    # Toss rows (hk 6322) keep UNKNOWN without a calendar; the ingest vocabulary is
    # unchanged for them (the Python collector writes Toss, not this surface).
    assert session_segment_of("us", "TOSS_INTEGRATED", _start_kst("11:00")) == UNKNOWN
    decision = _decide(venue="TOSS_INTEGRATED", source="TOSS", value=None)
    assert decision.reason == mi.REASON_UNKNOWN_VENUE


# ----------------------------------------------------------- migration shape ----


def _load(path: Path, name: str):
    spec = util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chain_and_expand_contract_marker() -> None:
    expand = _load(MIGRATION, "hk7037_expand")
    validate = _load(MIGRATION_VAL, "hk7037_validate")
    assert expand.down_revision == "20261127_hk6991_intent_v3"
    assert validate.down_revision == expand.revision
    assert len(expand.revision) <= 32 and len(validate.revision) <= 32
    for path in (MIGRATION, MIGRATION_VAL):
        assert "# expand-contract: contract-step 7037" in path.read_text()


def test_venue_check_adds_kis_day_not_valid_and_validation_is_the_follow_up() -> None:
    expand = _load(MIGRATION, "hk7037_expand_sql")
    captured: list[str] = []
    expand.op = SimpleNamespace(execute=lambda sql: captured.append(str(sql)))
    expand.upgrade()
    joined = " ".join(" ".join(captured).split())
    assert "ADD CONSTRAINT ck_research_us_candles_1m_venue" in joined
    assert "'KIS_DAY'" in joined and "'TOSS_INTEGRATED'" in joined
    assert "NOT VALID" in joined
    assert "VALIDATE CONSTRAINT" not in joined
    assert "ALTER COLUMN" not in joined  # no column change in this step

    validate = _load(MIGRATION_VAL, "hk7037_validate_sql")
    captured_v: list[str] = []
    validate.op = SimpleNamespace(execute=lambda sql: captured_v.append(str(sql)))
    validate.upgrade()
    assert any(
        "VALIDATE CONSTRAINT ck_research_us_candles_1m_venue" in s for s in captured_v
    )


def test_downgrade_refuses_while_kis_day_rows_exist_and_restores_the_old_list() -> None:
    expand = _load(MIGRATION, "hk7037_down_sql")
    captured: list[str] = []
    expand.op = SimpleNamespace(execute=lambda sql: captured.append(str(sql)))
    expand.downgrade()
    guard = captured[0]
    assert "venue = 'KIS_DAY'" in guard and "restrict_violation" in guard
    restored = " ".join(captured[1:])
    assert "'KIS_DAY'" not in restored
    assert "'TOSS_INTEGRATED'" in restored and "NOT VALID" in restored


def test_alembic_has_one_head_and_it_is_the_kis_day_validation() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    heads = script.get_heads()
    assert heads == ["20261127_hk7092_run_reasons"], heads
