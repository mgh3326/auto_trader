"""hk 6503: Naver status-list parser against the at-gap reference fixture."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.naver_status import (
    NaverStatusShapeError,
    parse_naver_status_payload,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "kr_trading_flags"
    / "naver_management_sample.json"
)


def _load() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by(obs: list[v.FlagObservation], symbol: str, category: str):
    matches = [o for o in obs if o.symbol == symbol and o.category == category]
    assert len(matches) == 1, (symbol, category, matches)
    return matches[0]


def test_fixture_224060_management_date_reason_and_alert_01() -> None:
    """Acceptance 1: 더코디 — 관리 with date and reason, alert 01 -> 투자주의."""
    obs, skipped = parse_naver_status_payload(_load())
    assert skipped == 0

    management = _by(obs, "224060", v.CATEGORY_MANAGEMENT)
    assert management.state == "designated"
    assert management.reason_code == "6045"
    assert management.effective_date == date(2026, 11, 25)
    assert management.raw_code == "1"
    assert management.source == v.SOURCE_NAVER_INTRADAY

    alert = _by(obs, "224060", v.CATEGORY_MARKET_ALERT)
    assert alert.state == "caution"
    assert alert.raw_code == "01"

    halt = _by(obs, "224060", v.CATEGORY_TRADING_HALT)
    assert halt.state == "none"
    assert halt.raw_code == "N"


def test_halted_row_031860_carries_halt_date_and_reason_and_raw_management() -> None:
    obs, _ = parse_naver_status_payload(_load())

    halt = _by(obs, "031860", v.CATEGORY_TRADING_HALT)
    assert halt.state == "halted"
    assert halt.reason_code == "625"
    assert halt.effective_date == date(2026, 10, 14)

    # manageStatusGb=3 is not a documented code: stored raw, never guessed.
    management = _by(obs, "031860", v.CATEGORY_MANAGEMENT)
    assert management.state == v.STATE_UNMAPPED
    assert management.raw_code == "3"


def test_no_delisting_observation_is_ever_emitted() -> None:
    """The Naver list has no 정리매매 field; absence must not become a value."""
    obs, _ = parse_naver_status_payload(_load())
    assert not [o for o in obs if o.category == v.CATEGORY_DELISTING_TRADE]


def test_unknown_market_alert_code_is_kept_raw() -> None:
    obs, _ = parse_naver_status_payload(
        [{"itemcode": "005930", "marketAlertType": "09"}]
    )
    (alert,) = obs
    assert alert.state == v.STATE_UNMAPPED
    assert alert.raw_code == "09"


def test_missing_field_yields_no_observation_not_false() -> None:
    """Absent equals unknown: a row without tradeStopYn says nothing about halts."""
    obs, _ = parse_naver_status_payload([{"itemcode": "005930"}])
    assert obs == []


def test_bad_symbol_rows_are_skipped_and_counted() -> None:
    obs, skipped = parse_naver_status_payload(
        [{"itemcode": "12"}, "not-a-row", {"itemcode": "224060", "tradeStopYn": "N"}]
    )
    assert skipped == 2
    assert [o.symbol for o in obs] == ["224060"]


def test_wrapped_stocks_list_is_accepted() -> None:
    obs, _ = parse_naver_status_payload({"stocks": _load()})
    assert _by(obs, "224060", v.CATEGORY_MARKET_ALERT).state == "caution"


@pytest.mark.parametrize("payload", [{"error": "x"}, "text", None, 7])
def test_non_list_shape_raises_and_writes_nothing(payload: object) -> None:
    with pytest.raises(NaverStatusShapeError):
        parse_naver_status_payload(payload)
