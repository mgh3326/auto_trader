"""Pinned NHPLUG live read-only period-quote client.

This is intentionally separate from the mock-only NHPLUG foundation.  It owns
the live REST hostname for this bounded surface and exposes only three period
quote calls.  There is no generic endpoint dispatcher, no credential-bearing
redirect, and no identity-scoped request shape.

The four safety layers are:

1. ``NHPLUG_LIVE_QUOTES_ENABLED`` is false unless explicitly armed and is
   checked at dispatch time.
2. The data path set is exact and checked before token cache access or I/O.
3. The HTTPS scheme, hostname, port, and resolved path are checked again after
   request construction immediately before ``send``.
4. This module has no identity-scoped API or identifier input.  The static
   guard makes future regressions visible in CI.

Vendor documentation says token reissuance raises a security alert.  A 0600
file cache is therefore shared by CLI invocations, expires from ``expires_in``,
and is invalidated only after a data response with HTTP 401.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import httpx

LIVE_BASE_URL: Final[str] = "https://api.nhplug.com:8443"
LIVE_HOST: Final[str] = "api.nhplug.com"
LIVE_PORT: Final[int] = 8443
LIVE_TOKEN_PATH: Final[str] = "/oauth2/token"

# Keep this set small, literal, and physically local to the live quote client.
# The collector has no host ownership and cannot add a route by configuration.
# `krstockQuotePeriod` in the vendor-maintained krstock OpenAPI document.
KR_PERIOD_PATH: Final[str] = "/krstock/quote/v1/period"
US_PERIOD_PATH: Final[str] = "/gbstock/quote/v1/period"
INDEXFX_PERIOD_PATH: Final[str] = "/gbstock/quote/v1/symbolIndexFxPeriod"
ALLOWED_DATA_PATHS: Final[frozenset[str]] = frozenset(
    {KR_PERIOD_PATH, US_PERIOD_PATH, INDEXFX_PERIOD_PATH}
)

LIVE_QUOTES_GATE_ENV: Final[str] = "NHPLUG_LIVE_QUOTES_ENABLED"
MIN_RATE_SECONDS: Final[float] = 0.2
DEFAULT_RATE_SECONDS: Final[float] = 0.25
MAX_BARS_PER_REQUEST: Final[int] = 9_999
TOKEN_REFRESH_LEEWAY_SECONDS: Final[float] = 60.0
DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0


class NHPlugLiveQuotesError(RuntimeError):
    """Base class for redacted live period-quote failures."""


class NHPlugLiveQuotesDisabled(NHPlugLiveQuotesError):
    """Raised unless the explicit live quote gate is armed."""


class NHPlugLiveQuotesConfigurationError(NHPlugLiveQuotesError):
    """Raised for malformed scoped configuration or token cache state."""


class NHPlugLiveQuotesEndpointError(NHPlugLiveQuotesError):
    """Raised before a request could leave the exact read-only boundary."""


class NHPlugLiveQuotesResponseError(NHPlugLiveQuotesError):
    """Raised for a malformed response without rendering its body."""


def _live_quotes_enabled() -> bool:
    return os.getenv(LIVE_QUOTES_GATE_ENV, "").strip().lower() == "true"


def _assert_live_quotes_enabled(scoped_enabled: bool | None = None) -> None:
    enabled = _live_quotes_enabled() if scoped_enabled is None else scoped_enabled
    if not enabled:
        raise NHPlugLiveQuotesDisabled(
            "NHPLUG live period quotes are disabled; set "
            "NHPLUG_LIVE_QUOTES_ENABLED=true"
        )


def _assert_data_path(path: str) -> None:
    if path not in ALLOWED_DATA_PATHS:
        raise NHPlugLiveQuotesEndpointError(
            "NHPLUG live data path is not in the period-quote allowlist"
        )


def _assert_resolved_live_request(
    request: httpx.Request, *, allowed_paths: frozenset[str]
) -> None:
    """Check the fully built request in the instruction before ``send``."""

    if (
        request.url.scheme != "https"
        or request.url.host != LIVE_HOST
        or request.url.port != LIVE_PORT
    ):
        raise NHPlugLiveQuotesEndpointError(
            "NHPLUG live request resolved outside the pinned HTTPS endpoint"
        )
    if request.url.path not in allowed_paths:
        raise NHPlugLiveQuotesEndpointError(
            "NHPLUG live request resolved outside the allowlisted path set"
        )


def _assert_nonempty(value: str, *, env_key: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise NHPlugLiveQuotesConfigurationError(f"{env_key} is required")
    return normalized


def _assert_date(value: str, *, name: str) -> str:
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise NHPlugLiveQuotesConfigurationError(f"{name} must be YYYYMMDD")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise NHPlugLiveQuotesConfigurationError(
            f"{name} must be a calendar date"
        ) from exc
    return value


def _assert_bars(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NHPlugLiveQuotesConfigurationError("bars must be an integer")
    if not 1 <= value <= MAX_BARS_PER_REQUEST:
        raise NHPlugLiveQuotesConfigurationError(
            f"bars must be between 1 and {MAX_BARS_PER_REQUEST}"
        )
    return value


def _assert_kr_symbol(value: str) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
        raise NHPlugLiveQuotesConfigurationError(
            "KR symbol must be an exact six-digit code"
        )
    return value


def _assert_quote_symbol(value: str, *, name: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > 15
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        raise NHPlugLiveQuotesConfigurationError(f"{name} is malformed")
    return normalized


class _FileTokenCache:
    """A small, 0600 JSON cache with atomic replacement and no token logging."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _assert_existing_file_is_private(self) -> None:
        if self._path.is_symlink():
            raise NHPlugLiveQuotesConfigurationError(
                "token cache must not be a symlink"
            )
        try:
            file_stat = self._path.stat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(file_stat.st_mode):
            raise NHPlugLiveQuotesConfigurationError(
                "token cache must be a regular file"
            )
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise NHPlugLiveQuotesConfigurationError(
                "token cache file mode must be 0600"
            )

    def load_valid(self, *, now: float) -> str | None:
        self._assert_existing_file_is_private()
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            token = raw["access_token"]
            expires_at = float(raw["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(token, str) or not token.strip():
            return None
        if now >= expires_at - TOKEN_REFRESH_LEEWAY_SECONDS:
            return None
        return token

    def store(self, *, token: str, expires_at: float) -> None:
        if self._path.is_symlink():
            raise NHPlugLiveQuotesConfigurationError(
                "token cache must not be a symlink"
            )
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        payload = json.dumps(
            {"access_token": token, "expires_at": expires_at},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.write(file_descriptor, payload)
            os.write(file_descriptor, b"\n")
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.replace(temporary, self._path)
            self._path.chmod(0o600)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if temporary.exists():
                temporary.unlink()

    def invalidate_if_matching(self, *, token: str) -> None:
        self._assert_existing_file_is_private()
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if raw.get("access_token") == token:
            self._path.unlink()


class NHPlugLiveQuotesClient:
    """Live data client with three typed period calls and no arbitrary route API."""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        token_cache_path: Path | str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        quotes_enabled: bool | None = None,
    ) -> None:
        self._app_key = _assert_nonempty(app_key, env_key="NHPLUG_LIVE_APP_KEY")
        self._app_secret = _assert_nonempty(
            app_secret, env_key="NHPLUG_LIVE_APP_SECRET"
        )
        self._token_cache = _FileTokenCache(Path(token_cache_path))
        self._transport = transport
        self._timeout = float(timeout)
        self._quotes_enabled = quotes_enabled

    @classmethod
    def from_scoped_env(
        cls,
        *,
        token_cache_path: Path | str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> NHPlugLiveQuotesClient:
        return cls(
            app_key=os.getenv("NHPLUG_LIVE_APP_KEY", ""),
            app_secret=os.getenv("NHPLUG_LIVE_APP_SECRET", ""),
            token_cache_path=token_cache_path,
            transport=transport,
        )

    async def _issue_token(self) -> str:
        """Issue only after a cache miss or a data-side 401 invalidation."""

        form = {
            "appkey": self._app_key,
            "appsecretkey": self._app_secret,
            "grant_type": "client_credentials",
            "scope": "oob",
        }
        async with httpx.AsyncClient(
            base_url=LIVE_BASE_URL,
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
        ) as client:
            request = client.build_request(
                "POST",
                LIVE_TOKEN_PATH,
                params=form,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            _assert_resolved_live_request(
                request, allowed_paths=frozenset({LIVE_TOKEN_PATH})
            )
            response = await client.send(request)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise NHPlugLiveQuotesResponseError(
                "NHPLUG live token response was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise NHPlugLiveQuotesResponseError(
                "NHPLUG live token response was not an object"
            )
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token.strip():
            raise NHPlugLiveQuotesResponseError(
                "NHPLUG live token response did not contain an access token"
            )
        if isinstance(expires_in, bool):
            raise NHPlugLiveQuotesResponseError(
                "NHPLUG live token response has an invalid expiry"
            )
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError) as exc:
            raise NHPlugLiveQuotesResponseError(
                "NHPLUG live token response has an invalid expiry"
            ) from exc
        if ttl <= TOKEN_REFRESH_LEEWAY_SECONDS:
            raise NHPlugLiveQuotesResponseError(
                "NHPLUG live token response expiry is too short to cache safely"
            )
        normalized = token.strip()
        self._token_cache.store(token=normalized, expires_at=time.time() + ttl)
        return normalized

    async def _get_token(self, *, invalid_token: str | None = None) -> str:
        now = time.time()
        cached = self._token_cache.load_valid(now=now)
        if cached is not None and cached != invalid_token:
            return cached
        if invalid_token is not None:
            self._token_cache.invalidate_if_matching(token=invalid_token)
        return await self._issue_token()

    async def _post_data(self, *, path: str, input_0: dict[str, str]) -> dict[str, Any]:
        """Issue one data request after every independent boundary check."""

        _assert_live_quotes_enabled(self._quotes_enabled)
        _assert_data_path(path)
        token = await self._get_token()
        for attempt in range(2):
            async with httpx.AsyncClient(
                base_url=LIVE_BASE_URL,
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                request = client.build_request(
                    "POST",
                    path,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=UTF-8",
                    },
                    json={"Input_0": input_0},
                )
                _assert_resolved_live_request(request, allowed_paths=ALLOWED_DATA_PATHS)
                response = await client.send(request)
            if response.status_code == 401 and attempt == 0:
                # The vendor documents 401 as the sole allowed reissuance case.
                token = await self._get_token(invalid_token=token)
                continue
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise NHPlugLiveQuotesResponseError(
                    "NHPLUG live quote response was not JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise NHPlugLiveQuotesResponseError(
                    "NHPLUG live quote response was not an object"
                )
            return dict(payload)
        raise NHPlugLiveQuotesResponseError(
            "NHPLUG live quote rejected the reissued token"
        )

    async def fetch_kr_period(
        self,
        *,
        symbol: str,
        end_date: str,
        bars: int,
        market_code: str = "KRX",
        market_classification: str = "1",
    ) -> dict[str, Any]:
        """Fetch domestic daily rows with all documented period fields explicit."""

        normalized_symbol = _assert_kr_symbol(symbol)
        normalized_end_date = _assert_date(end_date, name="end_date")
        normalized_bars = _assert_bars(bars)
        if market_code not in {"KRX", "NXT", "UNT"}:
            raise NHPlugLiveQuotesConfigurationError(
                "market_code must be KRX, NXT, or UNT"
            )
        if market_classification not in {"1", "4", "A", "E", "T"}:
            raise NHPlugLiveQuotesConfigurationError(
                "market_classification is malformed"
            )
        return await self._post_data(
            path=KR_PERIOD_PATH,
            input_0={
                "market_cd": market_code,
                "iem_cd": normalized_symbol,
                "mrkt_div_cls_code": market_classification,
                "edate": normalized_end_date,
                "array_cnt": f"{normalized_bars:04d}",
                "maxavg": "000",
                "gubun": "1",
                "xtick": "000",
                "today_cls_code": "0",
                "fake_tick": "1",
                "sur_flag": "0",
                "sur_gb_day_cnt": "00",
                "sur_bf_end_time": "000000",
                "out1_scale_change": "0",
                "out2_scale_change": "0",
            },
        )

    async def fetch_us_period(
        self, *, symbol: str, end_date: str, bars: int
    ) -> dict[str, Any]:
        """Fetch overseas individual-symbol daily rows with an explicit end date."""

        normalized_symbol = _assert_quote_symbol(symbol, name="US symbol")
        normalized_end_date = _assert_date(end_date, name="end_date")
        normalized_bars = _assert_bars(bars)
        return await self._post_data(
            path=US_PERIOD_PATH,
            input_0={
                "iem_cd": normalized_symbol,
                "end_dt": normalized_end_date,
                "count": f"{normalized_bars:04d}",
                "maxavg": "000",
                "gubun": "3",
                "xtick": "0001",
                "today_cls": "0",
                "market_cls": "1",
            },
        )

    async def fetch_index_fx_period(
        self, *, symbol: str, end_date: str, bars: int
    ) -> dict[str, Any]:
        """Fetch index or FX daily rows with its distinct documented field names."""

        normalized_symbol = _assert_quote_symbol(symbol, name="index or FX symbol")
        normalized_end_date = _assert_date(end_date, name="end_date")
        normalized_bars = _assert_bars(bars)
        return await self._post_data(
            path=INDEXFX_PERIOD_PATH,
            input_0={
                "iem_cd": normalized_symbol,
                "end_dt": normalized_end_date,
                "array_cnt": f"{normalized_bars:04d}",
                "maxavg": "000",
                "gubun": "1",
                "xtick": "001",
                "today_cls": "0",
                "scale_change": "0",
            },
        )
