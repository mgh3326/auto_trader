import asyncio
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from app.services import us_regular_session_raw_capture as capture_module


class FixtureClient:
    def __init__(self, responses: dict[str, httpx.Response] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((url, kwargs))
        response = self.responses.get(url)
        if response is None:
            return httpx.Response(
                200, json={"quote": {"bp": 100, "t": "2026-07-29T15:55:00Z"}}
            )
        return response


class OrderedFixtureClient(FixtureClient):
    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((url, kwargs))
        # Yield every quote request so all three are scheduled before any returns.
        await asyncio.sleep(0)
        return self.responses.get(
            url,
            httpx.Response(
                200, json={"quote": {"bp": 100, "t": "2026-07-29T15:55:00Z"}}
            ),
        )


class HangingFixtureClient(FixtureClient):
    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((url, kwargs))
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.fixture(autouse=True)
def _configured_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ALPACA_PAPER_API_KEY",
        "ALPACA_PAPER_API_SECRET",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCESS_TOKEN",
    ):
        monkeypatch.setenv(key, "fixture-value")


def test_write_append_only_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    capture_module._write_append_only(target, b"first")
    with pytest.raises(FileExistsError):
        capture_module._write_append_only(target, b"second")
    assert target.read_bytes() == b"first"


def test_secret_redaction_covers_headers_and_json_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "super-secret-key")
    response = httpx.Response(
        403,
        headers={"x-request-id": "safe", "set-cookie": "bad", "x-appkey": "no-save"},
        json={
            "message": "token=private-token",
            "api_key": "do-not-save",
            "appkey": "also-do-not-save",
            "x_appkey": "still-do-not-save",
        },
    )
    client = FixtureClient(
        {"https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest": response}
    )
    path = asyncio.run(
        capture_module.capture(symbols=("AAPL",), artifact_root=tmp_path, client=client)
    )[0]
    text = path.read_text()
    assert "super-secret-key" not in text
    assert "private-token" not in text
    assert "do-not-save" not in text
    assert "also-do-not-save" not in text
    assert "still-do-not-save" not in text
    assert "no-save" not in text
    assert "set-cookie" not in text.lower()


def test_raw_and_sanitized_body_hashes_are_preserved_separately(tmp_path: Path) -> None:
    raw = b'{"appkey":"redact-me","value":1}'
    response = httpx.Response(200, content=raw)
    client = FixtureClient(
        {"https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest": response}
    )
    record = json.loads(
        asyncio.run(
            capture_module.capture(
                symbols=("AAPL",), artifact_root=tmp_path, client=client
            )
        )[0].read_text()
    )
    assert record["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        record["stored_body_sha256"]
        == hashlib.sha256(record["body"].encode()).hexdigest()
    )
    assert record["raw_response_sha256"] != record["stored_body_sha256"]
    assert "redact-me" not in record["body"]


def test_alpaca_sip_entitlement_is_unavailable_without_fallback(tmp_path: Path) -> None:
    quote_url = "https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest"
    client = FixtureClient(
        {
            quote_url: httpx.Response(
                403, json={"message": "SIP subscription entitlement required"}
            )
        }
    )
    paths = asyncio.run(
        capture_module.capture(symbols=("AAPL",), artifact_root=tmp_path, client=client)
    )
    records = [json.loads(path.read_text()) for path in paths]
    quote = next(row for row in records if row["product"] == "latest_quote")
    assert quote["outcome"] == "unavailable"
    alpaca_calls = [call for call in client.calls if "alpaca" in call[0]]
    assert len(alpaca_calls) == 3
    assert all(call[1]["params"].get("feed") == "sip" for call in alpaca_calls)
    assert all("iex" not in call[0].lower() for call in client.calls)


def test_recent_sip_unavailable_and_historical_sip_success_are_separate_attempts(
    tmp_path: Path,
) -> None:
    base = "https://data.alpaca.markets/v2/stocks/AAPL"
    client = FixtureClient(
        {
            f"{base}/quotes/latest": httpx.Response(
                403,
                json={
                    "message": "subscription does not permit querying recent SIP data"
                },
            ),
            f"{base}/bars/latest": httpx.Response(
                403,
                json={
                    "message": "subscription does not permit querying recent SIP data"
                },
            ),
            f"{base}/bars": httpx.Response(
                200,
                json={
                    "bars": [
                        {"t": f"2026-07-29T09:{minute:02}:00-04:00"}
                        for minute in range(30, 37)
                    ]
                },
            ),
        }
    )
    paths = asyncio.run(
        capture_module.capture(
            symbols=("AAPL",),
            artifact_root=tmp_path,
            historical_sip_date=date(2026, 7, 29),
            client=client,
        )
    )
    records = {
        json.loads(path.read_text())["product"]: json.loads(path.read_text())
        for path in paths
    }
    assert records["latest_quote"]["outcome"] == "unavailable"
    assert records["latest_bar"]["outcome"] == "unavailable"
    historical = records["historical_1m_bars"]
    assert historical["outcome"] == "success"
    assert historical["request_params"] == {
        "timeframe": "1Min",
        "start": "2026-07-29T09:30:00-04:00",
        "end": "2026-07-29T09:37:00-04:00",
        "feed": "sip",
    }
    assert (
        "does not establish first appearance"
        in historical["historical_reread"]["statement"]
    )
    assert all(
        call[1]["params"].get("feed") == "sip"
        for call in client.calls
        if "alpaca" in call[0]
    )
    assert all("iex" not in call[0].lower() for call in client.calls)


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"chart": {"result": [1]}}, "success"),
        (401, {"message": "bad credentials"}, "auth"),
        (200, {}, "empty"),
        (500, {"message": "upstream failed"}, "error"),
    ],
)
def test_provider_outcome_taxonomy(status: int, payload: object, expected: str) -> None:
    assert capture_module._classification(status, json.dumps(payload)) == expected


def test_u06_shadow_is_observation_only_and_has_hypothetical_price(
    tmp_path: Path,
) -> None:
    client = OrderedFixtureClient()
    paths = asyncio.run(
        capture_module.capture(artifact_root=tmp_path, u06_shadow=True, client=client)
    )
    quote = json.loads(
        next(path for path in paths if "latest_quote" in path.name).read_text()
    )
    shadow = quote["u06_shadow"]
    assert shadow["sip_nbb"] == 100.0
    assert shadow["hypothetical_limit_price"] == 99.8
    assert shadow["participant_or_source_timestamp_raw"] == "2026-07-29T15:55:00Z"
    assert quote["provider_timestamp_raw"] == {"t": "2026-07-29T15:55:00Z"}
    assert "submission" in shadow["statement"]
    source = Path(capture_module.__file__).read_text()
    assert "execution_client" not in source
    assert "orders" not in source
    assert [url.split("/")[-3] for url, _ in client.calls[:3]] == [
        "AAPL",
        "IBM",
        "SPY",
    ]
    assert all("quotes/latest" in url for url, _ in client.calls[:3])
    assert capture_module.U06_DEADLINE_SECONDS < 140


def test_kis_exchange_codes_are_explicit_and_not_nas_for_all_symbols() -> None:
    assert capture_module.KIS_US_EXCHANGE_CODES == {
        "AAPL": "NAS",
        "IBM": "NYS",
        "SPY": "AMS",
    }
    requests = [
        next(
            request
            for request in capture_module._requests_for(symbol, date(2026, 7, 29))
            if request.provider == "kis"
        )
        for symbol in capture_module.DEFAULT_SYMBOLS
    ]
    assert [request.params["EXCD"] for request in requests] == ["NAS", "NYS", "AMS"]


def test_manifest_and_exit_are_nonzero_for_missing_credentials_or_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "ALPACA_PAPER_API_KEY",
        "ALPACA_PAPER_API_SECRET",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    client = FixtureClient()
    run = asyncio.run(
        capture_module.capture_run(
            symbols=("AAPL",), artifact_root=tmp_path, client=client
        )
    )
    manifest = json.loads(run.manifest_path.read_text())
    assert run.exit_code == 2
    assert manifest["exit_code"] == 2
    assert manifest["preflight_missing_credentials"]["alpaca"] == [
        "ALPACA_PAPER_API_KEY",
        "ALPACA_PAPER_API_SECRET",
    ]
    assert client.calls == []
    assert len(manifest["terminal_timeline"]) == 5
    assert {entry["outcome"] for entry in manifest["terminal_timeline"]} == {
        "preflight_blocked"
    }
    assert run.manifest_path.exists()


def test_manifest_exit_is_nonzero_for_provider_coverage_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "ALPACA_PAPER_API_KEY",
        "ALPACA_PAPER_API_SECRET",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCESS_TOKEN",
    ):
        monkeypatch.setenv(key, "fixture-value")
    client = FixtureClient(
        {
            "https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest": httpx.Response(
                403,
                json={
                    "message": "subscription does not permit querying recent SIP data"
                },
            )
        }
    )
    run = asyncio.run(
        capture_module.capture_run(
            symbols=("AAPL",), artifact_root=tmp_path, client=client
        )
    )
    manifest = json.loads(run.manifest_path.read_text())
    assert run.exit_code == 2
    assert manifest["preflight_missing_credentials"] == {}
    assert [
        {key: entry[key] for key in ("provider", "product", "symbol", "outcome")}
        for entry in manifest["coverage_missing_or_unsuccessful"]
    ] == [
        {
            "provider": "alpaca",
            "product": "latest_quote",
            "symbol": "AAPL",
            "outcome": "unavailable",
        }
    ]


def test_u06_deadline_preserves_terminal_timeline_for_all_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_module, "U06_DEADLINE_SECONDS", 0.01)
    client = HangingFixtureClient()
    run = asyncio.run(
        capture_module.capture_run(
            artifact_root=tmp_path, u06_shadow=True, client=client
        )
    )
    manifest = json.loads(run.manifest_path.read_text())
    assert run.exit_code == 2
    assert manifest["u06_deadline_exceeded"] is True
    assert manifest["run_started_utc"] <= manifest["run_finished_utc"]
    assert manifest["deadline_utc"]
    assert len(manifest["terminal_timeline"]) == 15
    assert (
        len(
            {
                (row["provider"], row["product"], row["symbol"])
                for row in manifest["terminal_timeline"]
            }
        )
        == 15
    )
    assert manifest["outcome_counts"] == {
        "deadline_cancelled": 3,
        "not_started_deadline": 12,
    }
    assert len(run.artifact_paths) == 3
    assert {json.loads(path.read_text())["outcome"] for path in run.artifact_paths} == {
        "deadline_cancelled"
    }


def test_timezone_conversion_uses_zoneinfo_not_hard_coded_offset() -> None:
    summer_et = datetime(2026, 7, 29, 15, 55, tzinfo=capture_module._NEW_YORK)
    winter_et = datetime(2026, 1, 29, 15, 55, tzinfo=capture_module._NEW_YORK)
    assert summer_et.utcoffset() != winter_et.utcoffset()
    assert (
        summer_et.astimezone(capture_module._SEOUL).hour
        != winter_et.astimezone(capture_module._SEOUL).hour
    )
    assert capture_module.kst_window_description() == "2026-07-29T19:54:50Z"
    assert capture_module.historical_sip_opening_window_params(date(2026, 1, 29))[
        "start"
    ].endswith("-05:00")
