# app/services/brokers/kiwoom/live_market_data.py
"""Kiwoom **live** read-only chart client (ka10080/ka10081/ka10082/ka10083).

Stage 1a of the Kiwoom live market-data effort. This module is the ONLY place
in the repo that may target ``https://api.kiwoom.com``. It exists so KR chart
data can eventually be compared between the mock and live hosts; it can read
charts and nothing else.

Safety boundary (every item is enforced in code and covered by a test):

1. Separate module. ``KiwoomMockClient`` and ``KiwoomAuthClient`` are neither
   extended nor modified — their mock-only assertions stay exactly as they are.
   This module deliberately does not import them, so the mock transport and the
   live transport share no code path at all.
2. No account number. This client never accepts, reads, stores, or exposes an
   account number, and it does not read ``KIWOOM_ACCOUNT_NO``.
   ⚠️ Strength of that guarantee: it prevents *accidental* order/account reach
   and makes a regression *statically detectable* (see the AST guard). It is
   NOT a structural impossibility proof — the value exists in the deployment
   env file, so a future edit that adds the setting could reach it.
3. api-id allowlist: only the four chart TRs. Checked before any I/O.
4. path allowlist: only ``/api/dostk/chart``. Checked before any I/O.
5. No order-constant reference. Order TRs (kt10000-kt10003) and the order path
   are never imported or named here.
6. Host pinned to the live base URL, then re-validated on the built request
   immediately before dispatch (mirrors what the mock client does for its own
   host).
7. Env gate ``KIWOOM_LIVE_MARKETDATA_ENABLED``, default false. Enforced at
   dispatch time, so even a directly-constructed client cannot send while the
   gate is off.

Credentials come from settings only (``KIWOOM_LIVE_APP_KEY`` /
``KIWOOM_LIVE_APP_SECRET``). No token, secret, or response body is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import uuid
from typing import Any, Final

import httpx
import redis.asyncio as redis

# Named imports only — never ``from ... import constants``. Binding the module
# would hand this file attribute access to the order TRs (constants.ORDER_*),
# which item 5 of the safety boundary forbids. The AST guard enforces this.
from app.services.brokers.kiwoom.constants import (
    CHART_DAILY_API_ID,
    CHART_MINUTE_API_ID,
    CHART_MONTHLY_API_ID,
    CHART_PATH,
    CHART_WEEKLY_API_ID,
    DEFAULT_TIMEOUT,
    HEADER_API_ID,
    HEADER_AUTHORIZATION,
    HEADER_CONT_YN,
    HEADER_NEXT_KEY,
    LIVE_BASE_URL,
    OAUTH_CONTENT_TYPE,
    OAUTH_GRANT_TYPE,
    OAUTH_PATH,
    SUCCESS_RETURN_CODE,
    TOKEN_REFRESH_LEEWAY_SECONDS,
)

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Allowlists (items 3 and 4)
# --------------------------------------------------------------------------

#: The only api-ids this client may ever send. Read-only chart TRs.
ALLOWED_API_IDS: Final[frozenset[str]] = frozenset(
    {
        CHART_MINUTE_API_ID,
        CHART_DAILY_API_ID,
        CHART_WEEKLY_API_ID,
        CHART_MONTHLY_API_ID,
    }
)

#: The only request paths this client may ever send.
ALLOWED_PATHS: Final[frozenset[str]] = frozenset({CHART_PATH})

#: ``upd_stkpc_tp`` (수정주가구분) values per the official chart docs.
ADJUSTED_PRICE_ON: Final[str] = "1"
ADJUSTED_PRICE_OFF: Final[str] = "0"

#: ``tic_scope`` (틱범위) values ka10080 documents.
ALLOWED_TIC_SCOPES: Final[frozenset[str]] = frozenset(
    {"1", "3", "5", "10", "15", "30", "45", "60"}
)

#: Response list keys, per the official docs.
DAILY_LIST_KEY: Final[str] = "stk_dt_pole_chart_qry"
MINUTE_LIST_KEY: Final[str] = "stk_min_pole_chart_qry"
WEEKLY_LIST_KEY: Final[str] = "stk_stk_pole_chart_qry"
MONTHLY_LIST_KEY: Final[str] = "stk_mth_pole_chart_qry"

_TOKEN_LOCK_TTL_SECONDS: Final[int] = 30
_TOKEN_WAIT_TIMEOUT_SECONDS: Final[float] = 5.0
_TOKEN_WAIT_POLL_SECONDS: Final[float] = 0.05


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class KiwoomLiveReadOnlyError(RuntimeError):
    """Base class for every live read-only refusal."""


class KiwoomLiveReadOnlyDisabled(KiwoomLiveReadOnlyError):
    """Raised when ``KIWOOM_LIVE_MARKETDATA_ENABLED`` is not true (item 7)."""


class KiwoomLiveReadOnlyConfigurationError(KiwoomLiveReadOnlyError):
    """Raised when live read-only config is incomplete."""


class KiwoomLiveReadOnlyEndpointError(KiwoomLiveReadOnlyError):
    """Raised when a base URL / resolved host other than live would be used."""


class KiwoomLiveReadOnlyApiIdError(KiwoomLiveReadOnlyError):
    """Raised when a non-chart api-id is requested (item 3)."""


class KiwoomLiveReadOnlyPathError(KiwoomLiveReadOnlyError):
    """Raised when a non-chart path is requested (item 4)."""


class KiwoomLiveTokenIssuanceUnavailable(KiwoomLiveReadOnlyError):
    """Raised when another issuer never publishes a usable token."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _live_host() -> str:
    host = httpx.URL(LIVE_BASE_URL).host
    if not host:  # pragma: no cover - LIVE_BASE_URL is a constant
        raise KiwoomLiveReadOnlyEndpointError("live base URL has no host")
    return host


def _validate_relative_path(path: str) -> None:
    """Reject absolute URLs, network-path references, and other odd shapes.

    Deliberately duplicated from the mock client rather than imported: this
    module keeps zero import edges to the mock transport (item 1), so the live
    path can never inherit a future change made for mock's benefit.
    """

    if (
        not path
        or not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or "\\" in path
        or "\r" in path
        or "\n" in path
    ):
        raise KiwoomLiveReadOnlyPathError(
            f"Kiwoom live request path must be a relative path; got {path!r}"
        )


def _assert_live_marketdata_enabled(scoped_enabled: bool | None = None) -> None:
    """Item 7: fail closed unless the operator armed the gate."""

    if scoped_enabled is None:
        from app.core.config import settings

        enabled = bool(getattr(settings, "kiwoom_live_marketdata_enabled", False))
    else:
        enabled = scoped_enabled
    if not enabled:
        raise KiwoomLiveReadOnlyDisabled(
            "Kiwoom live market data is disabled; set "
            "KIWOOM_LIVE_MARKETDATA_ENABLED=true to arm read-only chart access."
        )


def _client_fingerprint(app_key: str) -> str:
    import hashlib

    return hashlib.sha256(app_key.encode("utf-8")).hexdigest()[:16]


def _parse_expires_dt(value: str) -> float:
    """Kiwoom returns ``YYYYMMDDHHMMSS``; convert to a POSIX timestamp."""

    import datetime as dt

    parsed = dt.datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=dt.UTC)
    return parsed.timestamp()


# --------------------------------------------------------------------------
# Live OAuth (separate from KiwoomAuthClient, which stays mock-only)
# --------------------------------------------------------------------------


class KiwoomLiveReadOnlyAuthClient:
    """Live-host OAuth token issuance with a Redis single-flight cache.

    A deliberately separate implementation from ``KiwoomAuthClient``: that class
    asserts the mock host and must keep doing so (item 1). The Redis namespace
    is distinct too, so a live token can never be served to a mock caller or
    vice versa.
    """

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        redis_client: redis.Redis | None = None,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._transport = transport
        self._timeout = timeout
        self._redis_client = redis_client
        namespace = f"kiwoom:live-ro:oauth:{_client_fingerprint(app_key)}"
        self.token_key = f"{namespace}:access_token"
        self.lock_key = f"{namespace}:lock"

    async def get_token(self) -> str:
        cached = await self._get_cached_token()
        if cached is not None:
            return cached
        return await self._issue_single_flight()

    async def _get_redis(self) -> redis.Redis:
        if self._redis_client is not None:
            return self._redis_client
        redis_url = str(os.getenv("REDIS_URL", "")).strip()
        if not redis_url:
            raise KiwoomLiveReadOnlyConfigurationError(
                "Kiwoom live read-only Redis config missing: REDIS_URL"
            )
        return redis.from_url(
            redis_url,
            max_connections=20,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
            decode_responses=True,
        )

    async def _get_cached_token(self) -> str | None:
        redis_client = await self._get_redis()
        raw = await redis_client.get(self.token_key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            access_token = data["access_token"]
            expires_at = float(data["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if time.time() >= expires_at - TOKEN_REFRESH_LEEWAY_SECONDS:
            return None
        return str(access_token)

    async def _cache_token(self, *, access_token: str, expires_at: float) -> None:
        redis_client = await self._get_redis()
        ttl = max(int(expires_at - time.time()), 1)
        payload = {"access_token": access_token, "expires_at": expires_at}
        await redis_client.set(self.token_key, json.dumps(payload), ex=ttl)

    async def _issue_single_flight(self) -> str:
        redis_client = await self._get_redis()
        lock_token = str(uuid.uuid4())
        acquired = await redis_client.set(
            self.lock_key, lock_token, nx=True, ex=_TOKEN_LOCK_TTL_SECONDS
        )
        if acquired:
            try:
                cached = await self._get_cached_token()
                if cached is not None:
                    return cached
                access_token, expires_at = await self._issue_token()
                await self._cache_token(
                    access_token=access_token, expires_at=expires_at
                )
                _log.info("Kiwoom live read-only OAuth token issued and cached")
                return access_token
            finally:
                await self._release_lock(redis_client, lock_token)

        waited = await self._wait_for_cached_token()
        if waited is not None:
            return waited
        raise KiwoomLiveTokenIssuanceUnavailable(
            "Kiwoom live OAuth token issuance contended; "
            "no cached token after bounded wait"
        )

    async def _wait_for_cached_token(self) -> str | None:
        deadline = time.monotonic() + _TOKEN_WAIT_TIMEOUT_SECONDS
        while True:
            cached = await self._get_cached_token()
            if cached is not None:
                return cached
            if time.monotonic() >= deadline:
                return None
            poll = max(float(_TOKEN_WAIT_POLL_SECONDS), 0.0)
            await asyncio.sleep(poll + random.uniform(0.0, poll))

    async def _release_lock(self, redis_client: redis.Redis, lock_token: str) -> None:
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        else
            return 0
        end
        """
        try:
            await redis_client.eval(script, 1, self.lock_key, lock_token)
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "Kiwoom live OAuth lock release best-effort failure type=%s",
                type(exc).__name__,
            )

    async def _issue_token(self) -> tuple[str, float]:
        body = {
            "grant_type": OAUTH_GRANT_TYPE,
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        async with httpx.AsyncClient(
            base_url=LIVE_BASE_URL,
            transport=self._transport,
            timeout=self._timeout,
            # SHOULD-1: pinned explicitly, not inherited from the httpx default.
            # A redirect is the one way a validated request can still land on
            # another host/path after our checks have already passed.
            follow_redirects=False,
        ) as client:
            request = client.build_request(
                "POST",
                OAUTH_PATH,
                json=body,
                headers={"Content-Type": OAUTH_CONTENT_TYPE},
            )
            # Item 6 also applies to token issuance.
            if request.url.host != _live_host():
                raise KiwoomLiveReadOnlyEndpointError(
                    "Kiwoom live OAuth resolved to non-live host "
                    f"{request.url.host!r}; refusing to send."
                )
            # SHOULD-2: re-validate the resolved path too, so host and path are
            # checked symmetrically at the last moment before dispatch.
            if request.url.path != OAUTH_PATH:
                raise KiwoomLiveReadOnlyPathError(
                    "Kiwoom live OAuth resolved to unexpected path "
                    f"{request.url.path!r}; refusing to send."
                )
            response = await client.send(request)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if int(payload.get("return_code", -1)) != SUCCESS_RETURN_CODE:
            _log.warning(
                "Kiwoom live OAuth non-zero return_code=%s", payload.get("return_code")
            )
        token = str(payload.get("token") or "").strip()
        expires_raw = str(payload.get("expires_dt") or "").strip()
        if not token or not expires_raw:
            raise KiwoomLiveReadOnlyConfigurationError(
                "Kiwoom live OAuth response missing token/expires_dt"
            )
        return token, _parse_expires_dt(expires_raw)


# --------------------------------------------------------------------------
# Live read-only chart client
# --------------------------------------------------------------------------


class KiwoomLiveReadOnlyClient:
    """Read-only KRX chart access against ``https://api.kiwoom.com``.

    Deliberately has no account number and no order surface — see the module
    docstring for the strength of that guarantee.
    """

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        base_url: str = LIVE_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        marketdata_enabled: bool | None = None,
    ) -> None:
        if str(base_url).rstrip("/") != LIVE_BASE_URL:
            raise KiwoomLiveReadOnlyEndpointError(
                "KiwoomLiveReadOnlyClient only accepts the live base URL "
                f"({LIVE_BASE_URL}); refusing to use {base_url!r}."
            )
        self._base_url = str(base_url).rstrip("/")
        self._app_key = app_key
        self._app_secret = app_secret
        self._timeout = timeout
        self._marketdata_enabled = marketdata_enabled
        self._transport: httpx.BaseTransport | None = None
        self._token_override: str | None = None
        self._auth = KiwoomLiveReadOnlyAuthClient(
            app_key=app_key,
            app_secret=app_secret,
            timeout=timeout,
        )

    @classmethod
    def from_app_settings(cls) -> KiwoomLiveReadOnlyClient:
        from app.core.config import settings, validate_kiwoom_live_marketdata_config

        missing = validate_kiwoom_live_marketdata_config(settings)
        if missing:
            raise KiwoomLiveReadOnlyConfigurationError(
                "Kiwoom live market data is disabled or missing required "
                "configuration: " + ", ".join(missing)
            )
        return cls(
            base_url=str(settings.kiwoom_live_base_url).rstrip("/"),
            app_key=str(settings.kiwoom_live_app_key),
            app_secret=str(settings.kiwoom_live_app_secret),
            marketdata_enabled=bool(settings.kiwoom_live_marketdata_enabled),
        )

    @classmethod
    def from_scoped_env(cls) -> KiwoomLiveReadOnlyClient:
        """Build from only the live read-only keys loaded by the collector."""

        enabled = os.getenv("KIWOOM_LIVE_MARKETDATA_ENABLED") == "true"
        missing: list[str] = []
        if not enabled:
            missing.append("KIWOOM_LIVE_MARKETDATA_ENABLED=true")
        app_key = str(os.getenv("KIWOOM_LIVE_APP_KEY", "")).strip()
        app_secret = str(os.getenv("KIWOOM_LIVE_APP_SECRET", "")).strip()
        if not app_key:
            missing.append("KIWOOM_LIVE_APP_KEY")
        if not app_secret:
            missing.append("KIWOOM_LIVE_APP_SECRET")
        if missing:
            raise KiwoomLiveReadOnlyConfigurationError(
                "Kiwoom live read-only scoped config missing: " + ", ".join(missing)
            )
        return cls(
            base_url=str(os.getenv("KIWOOM_LIVE_BASE_URL", LIVE_BASE_URL)).rstrip("/"),
            app_key=app_key,
            app_secret=app_secret,
            marketdata_enabled=True,
        )

    def set_transport_for_test(
        self, transport: httpx.BaseTransport, *, token: str
    ) -> None:
        """Inject an httpx transport + pre-issued token for unit tests only."""

        self._transport = transport
        self._token_override = token

    # -- request path ------------------------------------------------------

    async def _resolve_token(self) -> str:
        if self._token_override is not None:
            return self._token_override
        return await self._auth.get_token()

    async def post_chart(
        self,
        *,
        api_id: str,
        body: dict[str, Any],
        path: str = CHART_PATH,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        """Send one chart request. Every guard runs before any I/O."""

        # Item 7 — gate first, so a disabled deployment never even resolves a
        # token, let alone opens a socket.
        _assert_live_marketdata_enabled(self._marketdata_enabled)

        # Item 3 — api-id allowlist.
        if api_id not in ALLOWED_API_IDS:
            raise KiwoomLiveReadOnlyApiIdError(
                f"Kiwoom live read-only client allows only chart api-ids "
                f"{sorted(ALLOWED_API_IDS)}; refusing api_id={api_id!r}."
            )

        # Item 4 — path allowlist (shape check first, so odd shapes are named
        # as path errors rather than falling through the allowlist).
        _validate_relative_path(path)
        if path not in ALLOWED_PATHS:
            raise KiwoomLiveReadOnlyPathError(
                f"Kiwoom live read-only client allows only {sorted(ALLOWED_PATHS)}; "
                f"refusing path={path!r}."
            )

        # Item 6 — base URL must still be live before we build anything.
        if self._base_url != LIVE_BASE_URL:
            raise KiwoomLiveReadOnlyEndpointError(
                "Kiwoom live request resolved to non-live base URL; refusing to send."
            )

        token = await self._resolve_token()

        headers = {
            HEADER_AUTHORIZATION: f"Bearer {token}",
            HEADER_API_ID: api_id,
            "Content-Type": OAUTH_CONTENT_TYPE,
        }
        if cont_yn is not None:
            headers[HEADER_CONT_YN] = cont_yn
        if next_key is not None:
            headers[HEADER_NEXT_KEY] = next_key

        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
            # SHOULD-1: pinned explicitly rather than inherited from the httpx
            # default. A 3xx is the one way an already-validated request can
            # still reach another host or path — and this host serves the order
            # API, so the redirect must die here, not be followed.
            follow_redirects=False,
        ) as client:
            request = client.build_request("POST", path, headers=headers, json=body)
            # Item 6 — re-validate the *resolved* host immediately before
            # dispatch, mirroring the mock client's own last-line check.
            if request.url.host != _live_host():
                raise KiwoomLiveReadOnlyEndpointError(
                    "Kiwoom live request resolved to non-live host "
                    f"{request.url.host!r}; refusing to send."
                )
            # SHOULD-2: the path was allowlisted pre-build, but only the host
            # was re-checked post-build. Remove that asymmetry — verify the
            # *resolved* path is still the chart path right before dispatch.
            if request.url.path != CHART_PATH:
                raise KiwoomLiveReadOnlyPathError(
                    "Kiwoom live request resolved to non-chart path "
                    f"{request.url.path!r}; refusing to send."
                )
            response = await client.send(request)

        response.raise_for_status()
        payload: dict[str, Any] = dict(response.json())
        payload["continuation"] = {
            "cont_yn": response.headers.get(HEADER_CONT_YN, ""),
            "next_key": response.headers.get(HEADER_NEXT_KEY, ""),
        }
        if int(payload.get("return_code", 0)) != SUCCESS_RETURN_CODE:
            _log.info(
                "Kiwoom live chart api_id=%s returned non-zero return_code=%s",
                api_id,
                payload.get("return_code"),
            )
        return payload

    # -- typed chart calls -------------------------------------------------

    async def fetch_daily_chart(
        self,
        *,
        symbol: str,
        base_dt: str,
        adjusted: bool = True,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        """ka10081 주식일봉차트조회요청. ``base_dt`` is YYYYMMDD (Required=Y)."""

        return await self.post_chart(
            api_id=CHART_DAILY_API_ID,
            body={
                "stk_cd": symbol,
                "base_dt": base_dt,
                "upd_stkpc_tp": ADJUSTED_PRICE_ON if adjusted else ADJUSTED_PRICE_OFF,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def fetch_minute_chart(
        self,
        *,
        symbol: str,
        tic_scope: str,
        base_dt: str | None = None,
        adjusted: bool = True,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        """ka10080 주식분봉차트조회요청. ``base_dt`` is optional (Required=N)."""

        if tic_scope not in ALLOWED_TIC_SCOPES:
            raise ValueError(
                f"tic_scope must be one of {sorted(ALLOWED_TIC_SCOPES, key=int)}; "
                f"got {tic_scope!r}"
            )
        body: dict[str, Any] = {
            "stk_cd": symbol,
            "tic_scope": tic_scope,
            "upd_stkpc_tp": ADJUSTED_PRICE_ON if adjusted else ADJUSTED_PRICE_OFF,
        }
        if base_dt is not None:
            body["base_dt"] = base_dt
        return await self.post_chart(
            api_id=CHART_MINUTE_API_ID,
            body=body,
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def fetch_weekly_chart(
        self,
        *,
        symbol: str,
        base_dt: str,
        adjusted: bool = True,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        """ka10082 주식주봉차트조회요청."""

        return await self.post_chart(
            api_id=CHART_WEEKLY_API_ID,
            body={
                "stk_cd": symbol,
                "base_dt": base_dt,
                "upd_stkpc_tp": ADJUSTED_PRICE_ON if adjusted else ADJUSTED_PRICE_OFF,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def fetch_monthly_chart(
        self,
        *,
        symbol: str,
        base_dt: str,
        adjusted: bool = True,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        """ka10083 주식월봉차트조회요청."""

        return await self.post_chart(
            api_id=CHART_MONTHLY_API_ID,
            body={
                "stk_cd": symbol,
                "base_dt": base_dt,
                "upd_stkpc_tp": ADJUSTED_PRICE_ON if adjusted else ADJUSTED_PRICE_OFF,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )
