# app/services/brokers/kiwoom/client.py
"""Kiwoom mock-only REST client (transport + post_api helper).

Mock-only: rejects any base URL other than ``constants.MOCK_BASE_URL`` and
refuses any per-call ``path`` that is not a relative path beginning with ``/``
so callers cannot smuggle in the live host. The token is fetched via
``KiwoomAuthClient`` and never logged or returned.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

import httpx

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.auth import KiwoomAuthClient

_log = logging.getLogger(__name__)

_AUTH_REJECT_RETURN_CODE: Final[int] = 8005
# ROB-733-style fail-closed bound: a loop counter, never recursive resubmission.
_MAX_TOKEN_REFRESH_RESUBMITS: Final[int] = 1
AUTH_STALE_TOKEN: Final[str] = "AUTH_STALE_TOKEN"
PROVIDER_REJECTED: Final[str] = "PROVIDER_REJECTED"
MALFORMED_RESPONSE: Final[str] = "MALFORMED_RESPONSE"
_CHART_READ_API_IDS: Final[frozenset[str]] = frozenset(
    {
        constants.CHART_MINUTE_API_ID,
        constants.CHART_DAILY_API_ID,
        constants.CHART_WEEKLY_API_ID,
        constants.CHART_MONTHLY_API_ID,
    }
)
# Explicit allowlist keeps every domestic/US order mutation out of token retry.
_AUTH_RETRY_READ_API_IDS: Final[frozenset[str]] = frozenset(
    _CHART_READ_API_IDS
    | {
        constants.ACCOUNT_ORDER_DETAIL_API_ID,
        constants.ACCOUNT_ORDER_STATUS_API_ID,
        constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID,
        constants.ACCOUNT_DEPOSIT_API_ID,
        constants.ACCOUNT_BALANCE_API_ID,
        constants.US_ACCOUNT_OPEN_ORDERS_API_ID,
        constants.US_ACCOUNT_POSITIONS_API_ID,
        constants.US_ACCOUNT_TODAY_ORDERS_API_ID,
        constants.US_ACCOUNT_DEPOSIT_DETAIL_API_ID,
        constants.US_ACCOUNT_FOREIGN_DEPOSIT_API_ID,
    }
)


def _is_auth_rejection(payload: dict[str, Any]) -> bool:
    """Recognize both documented and observed Kiwoom 8005 response shapes."""
    if str(payload.get("return_code", "")).strip() == str(_AUTH_REJECT_RETURN_CODE):
        return True
    return_msg = str(payload.get("return_msg") or "")
    return re.search(r"(?<!\d)8005(?!\d)", return_msg) is not None


def _strict_return_code(payload: dict[str, Any]) -> int | None:
    raw = payload.get("return_code")
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class KiwoomConfigurationError(RuntimeError):
    """Raised when Kiwoom mock config is incomplete or disabled."""


class KiwoomEndpointError(RuntimeError):
    """Raised when a non-mock base URL would be used."""


class KiwoomPreDispatchError(RuntimeError):
    """Raised when a request fails before HTTP dispatch can begin.

    Carries only redacted diagnostics safe for caller-facing responses.
    The chained ``__cause__`` is retained for INTERNAL tracebacks only;
    response builders must read exclusively the structured fields below and
    MUST NOT use ``str(exc)`` or ``exc.__cause__``.
    """

    def __init__(self, *, stage: str, api_id: str, cause_type: str) -> None:
        self.stage = stage
        self.api_id = api_id
        self.cause_type = cause_type
        self.dispatch_started: bool = False
        self.status: str = "not_submitted"
        super().__init__(
            f"Kiwoom request failed before dispatch "
            f"(stage={stage}, api_id={api_id}, cause_type={cause_type})"
        )


class KiwoomReadResponseError(RuntimeError):
    """Typed, value-redacted failure for a read-only provider response."""

    def __init__(
        self,
        *,
        reason_code: str,
        api_id: str,
        return_code: int | None,
        retry_disposition: str,
    ) -> None:
        self.reason_code = reason_code
        self.api_id = api_id
        self.return_code = return_code
        self.retry_disposition = retry_disposition
        super().__init__(
            "Kiwoom read response rejected "
            f"reason_code={reason_code} api_id={api_id} "
            f"return_code={return_code!r} retry={retry_disposition}"
        )


def _validate_relative_path(path: str) -> None:
    """Reject absolute URLs, network-path references, and other non-relative shapes.

    Network-path references like ``//api.kiwoom.com/...`` are dangerous because
    URL-join semantics let them re-target the host even though they look
    relative. Anything other than exactly one leading ``/`` followed by a
    non-``/`` character is rejected.
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
        raise ValueError(f"Kiwoom request path must be a relative path; got {path!r}")


class KiwoomMockClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        account_no: str,
        timeout: float = constants.DEFAULT_TIMEOUT,
        redis_settings: Any | None = None,
    ) -> None:
        if str(base_url).rstrip("/") != constants.MOCK_BASE_URL:
            raise KiwoomEndpointError(
                "KiwoomMockClient only accepts the mock base URL "
                f"({constants.MOCK_BASE_URL}); refusing to use {base_url!r}."
            )
        self._base_url = base_url.rstrip("/")
        self._app_key = app_key
        self._app_secret = app_secret
        self._account_no = account_no
        self._timeout = timeout
        self._transport: httpx.BaseTransport | None = None
        self._auth = KiwoomAuthClient(
            base_url=self._base_url,
            app_key=app_key,
            app_secret=app_secret,
            transport=None,
            timeout=timeout,
            redis_settings=redis_settings,
        )
        self._token_override: str | None = None

    @classmethod
    def from_app_settings(cls) -> KiwoomMockClient:
        from app.core.config import settings, validate_kiwoom_mock_config

        missing = validate_kiwoom_mock_config(settings)
        if missing:
            raise KiwoomConfigurationError(
                "Kiwoom mock account is disabled or missing required configuration: "
                + ", ".join(missing)
            )
        _assert_distinct_kr_us_identities(settings)
        return cls(
            base_url=str(settings.kiwoom_mock_base_url).rstrip("/"),
            app_key=str(settings.kiwoom_mock_app_key),
            app_secret=str(settings.kiwoom_mock_app_secret),
            account_no=str(settings.kiwoom_mock_account_no),
            redis_settings=settings,
        )

    def set_transport_for_test(
        self, transport: httpx.BaseTransport, *, token: str
    ) -> None:
        """Inject a httpx transport + pre-issued token for unit tests only."""

        self._transport = transport
        self._token_override = token

    @property
    def account_no(self) -> str:
        return self._account_no

    async def _resolve_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        if self._token_override is not None:
            return self._token_override
        if force_reissue:
            return await self._auth.get_token(
                force_reissue=True,
                failed_token=failed_token,
            )
        return await self._auth.get_token()

    async def _before_api_dispatch(self, api_id: str) -> None:
        """Provider-specific dispatch hook; the base mock client is unrestricted."""

        del api_id

    async def post_api(
        self,
        *,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        _validate_relative_path(path)
        if self._base_url != constants.MOCK_BASE_URL:
            raise ValueError(
                "Kiwoom mock request resolved to non-mock host; refusing to send."
            )

        try:
            token = await self._resolve_token()
        except Exception as exc:
            raise KiwoomPreDispatchError(
                stage="token_resolution",
                api_id=api_id,
                cause_type=type(exc).__name__,
            ) from exc

        token_refresh_resubmits = 0
        while True:
            response = await self._send_api_once(
                token=token,
                api_id=api_id,
                path=path,
                body=body,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            response.raise_for_status()
            payload: dict[str, Any] = dict(response.json())
            payload["continuation"] = {
                "cont_yn": response.headers.get(constants.HEADER_CONT_YN, ""),
                "next_key": response.headers.get(constants.HEADER_NEXT_KEY, ""),
            }
            if (
                api_id in _AUTH_RETRY_READ_API_IDS
                and _is_auth_rejection(payload)
                and token_refresh_resubmits < _MAX_TOKEN_REFRESH_RESUBMITS
            ):
                token_refresh_resubmits += 1
                try:
                    token = await self._resolve_token(
                        force_reissue=True,
                        failed_token=token,
                    )
                except Exception as exc:
                    raise KiwoomPreDispatchError(
                        stage="token_refresh",
                        api_id=api_id,
                        cause_type=type(exc).__name__,
                    ) from exc
                continue

            if api_id in _AUTH_RETRY_READ_API_IDS and _is_auth_rejection(payload):
                raise KiwoomReadResponseError(
                    reason_code=AUTH_STALE_TOKEN,
                    api_id=api_id,
                    return_code=_strict_return_code(payload),
                    retry_disposition="RETRIED_ONCE_THEN_STOP",
                )

            if api_id in _CHART_READ_API_IDS:
                return_code = _strict_return_code(payload)
                if return_code is None:
                    raise KiwoomReadResponseError(
                        reason_code=MALFORMED_RESPONSE,
                        api_id=api_id,
                        return_code=None,
                        retry_disposition="STOP_NO_RETRY",
                    )
                if return_code != constants.SUCCESS_RETURN_CODE:
                    raise KiwoomReadResponseError(
                        reason_code=PROVIDER_REJECTED,
                        api_id=api_id,
                        return_code=return_code,
                        retry_disposition="STOP_NO_RETRY",
                    )

            if int(payload.get("return_code", 0)) != constants.SUCCESS_RETURN_CODE:
                _log.info(
                    "Kiwoom api_id=%s returned non-zero return_code=%s",
                    api_id,
                    payload.get("return_code"),
                )
            return payload

    async def _send_api_once(
        self,
        *,
        token: str,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None,
        next_key: str | None,
    ) -> httpx.Response:
        stage = "pre_dispatch_hook"
        dispatch_started = False
        try:
            await self._before_api_dispatch(api_id)
            stage = "request_build"
            headers = {
                constants.HEADER_AUTHORIZATION: f"Bearer {token}",
                constants.HEADER_API_ID: api_id,
                "Content-Type": constants.OAUTH_CONTENT_TYPE,
            }
            if cont_yn is not None:
                headers[constants.HEADER_CONT_YN] = cont_yn
            if next_key is not None:
                headers[constants.HEADER_NEXT_KEY] = next_key

            async with httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                request = client.build_request("POST", path, headers=headers, json=body)
                stage = "host_validation"
                if request.url.host != httpx.URL(constants.MOCK_BASE_URL).host:
                    raise ValueError(
                        "Kiwoom mock request resolved to non-mock host "
                        f"{request.url.host!r}; refusing to send."
                    )
                dispatch_started = True
                response = await client.send(request)
        except Exception as exc:
            if not dispatch_started:
                raise KiwoomPreDispatchError(
                    stage=stage, api_id=api_id, cause_type=type(exc).__name__
                ) from exc
            raise
        return response


def _assert_distinct_kr_us_identities(settings_obj: Any) -> None:
    """Reject a KR factory wired to the US app identity or account.

    Exact equality is intentional: a matching configured identity is unsafe,
    while an absent optional opposite-lane setting is not inferred as a match.
    """

    kr_app_key = str(getattr(settings_obj, "kiwoom_mock_app_key", "") or "").strip()
    us_app_key = str(getattr(settings_obj, "kiwoom_mock_us_app_key", "") or "").strip()
    kr_account = str(getattr(settings_obj, "kiwoom_mock_account_no", "") or "").strip()
    us_account = str(
        getattr(settings_obj, "kiwoom_mock_us_account_no", "") or ""
    ).strip()
    if (us_app_key and kr_app_key == us_app_key) or (
        us_account and kr_account == us_account
    ):
        raise KiwoomConfigurationError(
            "Kiwoom KR and US mock credential/account identities must be distinct"
        )
