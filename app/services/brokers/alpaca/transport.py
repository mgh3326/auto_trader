from typing import Any, Protocol

import httpx


class HTTPTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response: ...


class HttpxTransport:
    def __init__(self, base_url: str, api_key: str, api_secret: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers["APCA-API-KEY-ID"] = self._api_key
        headers["APCA-API-SECRET-KEY"] = self._api_secret
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient() as client:
            return await client.request(method, url, headers=headers, **kwargs)
