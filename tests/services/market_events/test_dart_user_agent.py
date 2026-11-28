"""OpenDART redirects requests without a User-Agent to /error1.html (hk 7111)."""

from __future__ import annotations

from app.services.market_events import dart_capital


class _Response:
    status = 200

    def read(self, _limit: int) -> bytes:
        return b'{"status": "000"}'


class _Connection:
    seen: list[dict[str, str]] = []

    def __init__(self, host: str, timeout: int) -> None:
        self.host = host

    def request(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> None:
        _Connection.seen.append(dict(headers or {}))

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        pass


def test_get_bytes_sends_a_user_agent(monkeypatch) -> None:
    _Connection.seen.clear()
    monkeypatch.setattr(dart_capital, "HTTPSConnection", _Connection)
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", "k" * 40)
    assert (
        dart_capital._get_bytes("list.json", {"bgn_de": "20261127"})
        == b'{"status": "000"}'
    )
    assert (
        _Connection.seen
        and _Connection.seen[0].get("User-Agent") == dart_capital.USER_AGENT
    )
