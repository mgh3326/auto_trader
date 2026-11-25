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


def _regular(day: dt.date, close_hour: int = 16):
    et = cap.ET
    return (
        dt.datetime(day.year, day.month, day.day, 9, 30, tzinfo=et).astimezone(dt.UTC),
        dt.datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=et).astimezone(
            dt.UTC
        ),
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
    def bounds(day):
        return None if day.weekday() >= 5 else _regular(day)

    assert cap.us_session(utc, session_bounds=bounds) == expected


def test_holiday_is_outside_session() -> None:
    # Thanksgiving 2026-11-26, 10:00 ET: weekday but not an XNYS session.
    thanksgiving = dt.datetime(2026, 11, 26, 15, 0, tzinfo=dt.UTC)
    assert cap.us_session(thanksgiving, session_bounds=lambda d: None) is None


def test_early_close_day_ends_regular_at_real_close() -> None:
    # 2026-11-27 closes 13:00 ET: 14:00 ET is post-market, 17:30 ET is outside.
    def bounds(day):
        return _regular(day, close_hour=13)

    at_1400 = dt.datetime(2026, 11, 27, 19, 0, tzinfo=dt.UTC)
    at_1730 = dt.datetime(2026, 11, 27, 22, 30, tzinfo=dt.UTC)
    at_1200 = dt.datetime(2026, 11, 27, 17, 0, tzinfo=dt.UTC)
    assert cap.us_session(at_1200, session_bounds=bounds) == "us_regular"
    assert cap.us_session(at_1400, session_bounds=bounds) == "us_post"
    assert cap.us_session(at_1730, session_bounds=bounds) is None


def test_missing_block_or_instruments_is_an_error() -> None:
    run_js, _ = _fake({"DESC": {"rows": []}})  # ASC block missing
    out = cap.capture(
        session="us_pre", volume_floor=1, pages=1, run_js=run_js, now=lambda: FIXED
    )
    assert out["errors"] == {"down": "missing_block"}
    js = cap.build_js(
        change_field="changePercent", volume_field="dayVolume", volume_floor=1, pages=1
    )
    assert "no_instruments_array" in js and "j.instruments||[]" not in js


@pytest.mark.parametrize(
    "url",
    ["http://example.com/x", "ftp://h/x", "https://user:pw@h/x", "https:///x", ""],
)
def test_validate_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(cap.IngestConfigError):
        cap.validate_ingest(url, "tok", "X-H")


def test_validate_accepts_https_and_loopback_http() -> None:
    cap.validate_ingest("https://at.example/trading/api/x", "tok", "X-H")
    cap.validate_ingest("http://127.0.0.1:8000/x", "tok", "X-H")


def test_bad_token_error_never_contains_the_value() -> None:
    secret = "s3cr3t-value"
    with pytest.raises(cap.IngestConfigError) as info:
        cap.validate_ingest("https://h/x", secret + "\n", "X-H")
    assert secret not in str(info.value)
    assert "BENZINGA_PRO_INGEST_TOKEN" in str(info.value)


def test_redirects_are_not_followed() -> None:
    handler = cap._NoRedirect()
    assert (
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil/") is None
    )


def test_main_does_not_post_a_failed_side(tmp_path, monkeypatch) -> None:
    calls = {"post": 0}
    monkeypatch.setenv("BENZINGA_PRO_INGEST_URL", "https://h/x")
    monkeypatch.setenv("BENZINGA_PRO_INGEST_TOKEN", "tok")
    monkeypatch.setattr(
        cap,
        "capture",
        lambda **kw: {
            "payload": {
                "captured_at": "2026-11-25T14:00:00Z",
                "movers": {"up": [], "down": []},
            },
            "errors": {"up": 429},
        },
    )
    monkeypatch.setattr(
        cap, "post", lambda *a, **k: calls.__setitem__("post", calls["post"] + 1)
    )
    rc = cap.main(["--session", "us_pre", "--out-dir", str(tmp_path)])
    assert rc == 1 and calls["post"] == 0
    line = json.loads((tmp_path / "benzinga-pro-2026-11-25.jsonl").read_text())
    assert line["errors"] == {"up": 429}


def test_main_checks_ingest_config_before_capture(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BENZINGA_PRO_INGEST_URL", raising=False)
    monkeypatch.delenv("BENZINGA_PRO_INGEST_TOKEN", raising=False)

    def boom(**kw):
        raise AssertionError("capture must not run")

    monkeypatch.setattr(cap, "capture", boom)
    assert cap.main(["--session", "us_pre", "--out-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())
