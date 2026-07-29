import asyncio
import json
from datetime import datetime
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
        headers={"x-request-id": "safe", "set-cookie": "bad"},
        json={"message": "token=private-token", "api_key": "do-not-save"},
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
    assert "set-cookie" not in text.lower()


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
    assert len(alpaca_calls) == 2
    assert all(call[1]["params"] == {"feed": "sip"} for call in alpaca_calls)
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
    client = FixtureClient()
    paths = asyncio.run(
        capture_module.capture(
            symbols=("AAPL",), artifact_root=tmp_path, u06_shadow=True, client=client
        )
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


def test_timezone_conversion_uses_zoneinfo_not_hard_coded_offset() -> None:
    summer_et = datetime(2026, 7, 29, 15, 55, tzinfo=capture_module._NEW_YORK)
    winter_et = datetime(2026, 1, 29, 15, 55, tzinfo=capture_module._NEW_YORK)
    assert summer_et.utcoffset() != winter_et.utcoffset()
    assert (
        summer_et.astimezone(capture_module._SEOUL).hour
        != winter_et.astimezone(capture_module._SEOUL).hour
    )
    assert capture_module.kst_window_description() == "2026-07-29T19:54:50Z"
