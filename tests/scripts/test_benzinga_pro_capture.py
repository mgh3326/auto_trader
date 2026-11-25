"""hk 6573: Benzinga Pro capture script (fake aside, no network)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scripts import benzinga_pro_capture as cap

pytestmark = pytest.mark.unit

FIXED = dt.datetime(2026, 11, 25, 14, 12, 11, tzinfo=dt.UTC)


def _fake(result: dict):
    seen = {}

    def run_js(js: str, timeout: int) -> str:
        seen["js"] = js
        return "noise\n" + cap.RESULT_MARK + json.dumps(result) + "\n[ok]"

    return run_js, seen


def test_capture_maps_rows_and_excludes_no_session_price() -> None:
    result = {
        "DESC": {
            "total": 762,
            "error": None,
            "rows": [
                {
                    "symbol": "nrsn",
                    "preMarketChangePercent": 149.09,
                    "preMarketVolume": "30254723",
                    "price": 1.2,
                    "previousClose": 0.49,
                    "marketCap": "123",
                    "sector": "Health Care",
                }
            ],
        },
        "ASC": {
            "total": 761,
            "error": None,
            "rows": [
                {
                    "symbol": "AFL",
                    "preMarketChangePercent": -100,
                    "preMarketVolume": "59693",
                },
                {
                    "symbol": "DELL",
                    "preMarketChangePercent": -7.12,
                    "preMarketVolume": "2310000",
                },
            ],
        },
    }
    run_js, seen = _fake(result)
    out = cap.capture(
        session="us_pre", volume_floor=50000, pages=4, run_js=run_js, now=lambda: FIXED
    )
    p = out["payload"]
    assert p["schema"] == "benzinga-pro-capture/v1"
    assert p["captured_at"] == "2026-11-25T14:12:11Z"
    assert p["movers"]["up"][0] == {
        "symbol": "NRSN",
        "rank": 1,
        "change_pct": "149.09",
        "volume": "30254723",
        "price": "1.2",
        "previous_close": "0.49",
        "market_cap": "123",
        "sector": "Health Care",
        "excluded_reason": None,
    }
    down = p["movers"]["down"]
    assert (
        down[0]["symbol"] == "AFL" and down[0]["excluded_reason"] == "no_session_price"
    )
    assert down[1]["symbol"] == "DELL" and down[1]["excluded_reason"] is None
    assert p["breaking_events"] == []
    assert out["errors"] == {}
    assert "preMarketVolume > 50000" in seen["js"]


def test_payload_never_carries_a_key_field() -> None:
    run_js, _ = _fake({"DESC": {"rows": []}, "ASC": {"rows": []}})
    p = cap.capture(
        session="us_regular", volume_floor=0, pages=1, run_js=run_js, now=lambda: FIXED
    )["payload"]
    text = json.dumps(p).lower()
    assert "apikey" not in text and '"key"' not in text and "token" not in text


def test_js_returns_only_rows_never_the_key() -> None:
    js = cap.build_js(
        change_field="changePercent", volume_field="dayVolume", volume_floor=1, pages=1
    )
    # the key lives in local `k`; the returned object is built only from rows/total/error
    assert "return {error:'no_session_in_page'}" in js
    assert "return k" not in js and "apikey:" not in js and "key:" not in js
    assert "console.log" in js and js.count("console.log") == 1


def test_page_error_is_reported() -> None:
    run_js, _ = _fake({"DESC": {"error": 401, "rows": []}, "ASC": {"rows": []}})
    out = cap.capture(
        session="us_pre", volume_floor=1, pages=1, run_js=run_js, now=lambda: FIXED
    )
    assert out["errors"] == {"up": 401}


def test_no_session_in_page_raises() -> None:
    run_js, _ = _fake({"error": "no_session_in_page"})
    with pytest.raises(RuntimeError):
        cap.capture(
            session="us_pre", volume_floor=1, pages=1, run_js=run_js, now=lambda: FIXED
        )


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (dt.datetime(2026, 11, 25, 10, 0, tzinfo=dt.UTC), "us_pre"),  # 05:00 ET
        (dt.datetime(2026, 11, 25, 15, 0, tzinfo=dt.UTC), "us_regular"),  # 10:00 ET
        (dt.datetime(2026, 11, 25, 22, 0, tzinfo=dt.UTC), "us_post"),  # 17:00 ET
        (dt.datetime(2026, 11, 26, 2, 0, tzinfo=dt.UTC), None),  # 21:00 ET
        (dt.datetime(2026, 11, 28, 15, 0, tzinfo=dt.UTC), None),  # Saturday
    ],
)
def test_us_session(utc: dt.datetime, expected: str | None) -> None:
    assert cap.us_session(utc) == expected
