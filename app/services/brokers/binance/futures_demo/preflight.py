"""ROB-298 PR 2 — Binance Futures Demo read-only preflight client.

Non-mutating credential-presence check for the USD-M Futures Demo lane.
Calls ``GET /fapi/v1/account`` (read-only signed endpoint) and returns a
redacted summary that the smoke CLI can write to evidence JSON without
leaking secrets.

Mirrors ``spot_demo.preflight`` deliberately: per ROB-296 Hermes review
§1 (Option A), preserving environment-specific fail-closed isolation
outweighs deduplication across signed lanes.

What this module does NOT do:
  * Place, cancel, or query orders. Order submission is the execution
    client's job (PR 2 follow-up task) — this preflight is read-only.
  * Touch the database / ledger.
  * Activate scheduler / TaskIQ / Prefect / Hermes.
  * Route to live, testnet, or Spot Demo hosts (transport layer refuses
    with ``BinanceFuturesDemoCrossAllowlistViolation`` /
    ``BinanceLiveHostBlocked``).
  * Set leverage / margin type / position mode (those mutate account
    state and live on the execution client).

Fail-closed contract:
  * ``BINANCE_FUTURES_DEMO_ENABLED`` unset/non-truthy → ``BinanceFuturesDemoDisabled``.
  * Missing key/secret → ``BinanceFuturesDemoMissingCredentials``.
  * Base URL outside ``FUTURES_DEMO_HOSTS`` → ``BinanceLiveHostBlocked`` (or
    ``BinanceFuturesDemoCrossAllowlistViolation`` if it lands on a sibling
    allowlist: live spot, live futures, Spot Demo, or deprecated testnet).
  * Server rejects HMAC signature with codes -2014 / -2008 / -1022 →
    ``BinanceFuturesDemoUnsupportedAuth``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoMissingCredentials,
    BinanceFuturesDemoUnsupportedAuth,
)
from app.services.brokers.binance.futures_demo.signing import (
    BINANCE_FUTURES_DEMO_RECV_WINDOW_MS,
    _sign_request_params,
)
from app.services.brokers.binance.futures_demo.transport import (
    build_futures_demo_client,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL: Final[str] = "https://demo-fapi.binance.com"
_ACCOUNT_PATH: Final[str] = "/fapi/v1/account"

# Binance error codes that indicate the server rejected our HMAC-SHA256
# signature. If the operator's Futures Demo account requires Ed25519
# instead, the request reaches the server and gets one of these codes
# back — we surface that as an explicit "unsupported auth" exception
# rather than silently falling back.
_UNSUPPORTED_AUTH_BINANCE_CODES: frozenset[int] = frozenset({-2014, -2008, -1022})


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _redact_api_key(api_key: str) -> str:
    """Return a fingerprint of the API key safe for logs/evidence.

    The api_key half of the credential pair is less sensitive than the
    secret, but we still avoid full logging in case the evidence file is
    shared. Format: ``<first4>…<last2>`` (or ``***`` if too short).
    """
    if len(api_key) >= 6:
        return f"{api_key[:4]}…{api_key[-2:]}"
    return "***"


@dataclass(frozen=True, slots=True)
class FuturesDemoPreflightResult:
    """Source-labeled, secret-redacted preflight evidence.

    Designed to be serialized to JSON for the smoke CLI's evidence
    output. ``source`` / ``venue`` / ``product`` are explicit so the
    operator can distinguish Futures Demo evidence from Spot Demo
    evidence at a glance.
    """

    source: str  # "futures_demo"
    venue: str  # "binance"
    product: str  # "usdm_futures"
    base_url: str
    api_key_fingerprint: str
    account_can_trade: bool | None
    account_can_deposit: bool | None
    account_can_withdraw: bool | None
    account_type: str | None
    assets_nonzero_count: int
    positions_nonzero_count: int

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "venue": self.venue,
            "product": self.product,
            "base_url": self.base_url,
            "api_key_fingerprint": self.api_key_fingerprint,
            "account": {
                "can_trade": self.account_can_trade,
                "can_deposit": self.account_can_deposit,
                "can_withdraw": self.account_can_withdraw,
                "account_type": self.account_type,
                "assets_nonzero_count": self.assets_nonzero_count,
                "positions_nonzero_count": self.positions_nonzero_count,
            },
        }


def _count_nonzero_assets(payload: dict[str, Any]) -> int:
    """Count assets with nonzero ``walletBalance``.

    Binance Futures /account ``assets`` rows each carry a
    ``walletBalance`` (string). Drop everything else from the summary —
    we never want balance amounts in evidence files.
    """
    assets = payload.get("assets") or []
    nonzero = 0
    for entry in assets:
        try:
            balance = float(entry.get("walletBalance", "0"))
        except (TypeError, ValueError):
            balance = 0.0
        if balance != 0.0:
            nonzero += 1
    return nonzero


def _count_nonzero_positions(payload: dict[str, Any]) -> int:
    """Count positions with nonzero ``positionAmt``.

    A nonzero ``positionAmt`` means the account currently holds an open
    position on that symbol. Counting only is intentional: position size
    and entry price are sensitive PII-equivalent for a futures account.
    """
    positions = payload.get("positions") or []
    nonzero = 0
    for entry in positions:
        try:
            amt = float(entry.get("positionAmt", "0"))
        except (TypeError, ValueError):
            amt = 0.0
        if amt != 0.0:
            nonzero += 1
    return nonzero


def _summarize_account(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the safe-to-log summary fields from a Futures account payload.

    Drops every balance amount (only counts of nonzero rows are kept) and
    any field that could identify the account holder.
    """
    return {
        "can_trade": payload.get("canTrade"),
        "can_deposit": payload.get("canDeposit"),
        "can_withdraw": payload.get("canWithdraw"),
        "account_type": payload.get("accountType"),
        "assets_nonzero_count": _count_nonzero_assets(payload),
        "positions_nonzero_count": _count_nonzero_positions(payload),
    }


class FuturesDemoPreflightClient:
    """Read-only Futures Demo client used by the smoke CLI.

    Constructed via ``from_env()`` for the strict fail-closed path, or
    directly for tests / inline use.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        if not api_key:
            raise BinanceFuturesDemoMissingCredentials(
                "BINANCE_FUTURES_DEMO_API_KEY is empty. Refusing to construct "
                "Futures Demo preflight client."
            )
        if not api_secret:
            raise BinanceFuturesDemoMissingCredentials(
                "BINANCE_FUTURES_DEMO_API_SECRET is empty. Refusing to "
                "construct Futures Demo preflight client."
            )
        # Transport factory enforces the host-allowlist check on base_url
        # plus the cross-allowlist guard against Spot Demo / live / testnet.
        self._client = build_futures_demo_client(
            api_key=api_key, api_secret=api_secret, base_url=base_url
        )
        # _api_secret is the ONLY persistent reference to the secret.
        # repr/str/log paths MUST NOT read this attribute directly.
        self._api_secret = api_secret
        self._api_key = api_key
        self._base_url = base_url

    @classmethod
    def from_env(cls) -> FuturesDemoPreflightClient:
        """Construct from environment variables with full fail-closed checks.

        Env contract:
          * ``BINANCE_FUTURES_DEMO_ENABLED`` MUST be truthy.
          * ``BINANCE_FUTURES_DEMO_API_KEY`` MUST be present and non-empty.
          * ``BINANCE_FUTURES_DEMO_API_SECRET`` MUST be present and non-empty.
          * ``BINANCE_FUTURES_DEMO_BASE_URL`` (optional) MUST be a Futures
            Demo host if set; transport factory enforces.

        Note: neither ``BINANCE_TESTNET_*`` nor ``BINANCE_SPOT_DEMO_*`` env
        vars activate this path. They are read by their respective adapters.
        """
        if not _truthy(os.environ.get("BINANCE_FUTURES_DEMO_ENABLED")):
            raise BinanceFuturesDemoDisabled(
                "BINANCE_FUTURES_DEMO_ENABLED is not truthy. Set "
                "BINANCE_FUTURES_DEMO_ENABLED=true to opt in to the Futures "
                "Demo preflight path. Default is fail-closed."
            )
        api_key = os.environ.get("BINANCE_FUTURES_DEMO_API_KEY", "")
        api_secret = os.environ.get("BINANCE_FUTURES_DEMO_API_SECRET", "")
        if not api_key:
            raise BinanceFuturesDemoMissingCredentials(
                "BINANCE_FUTURES_DEMO_API_KEY is empty or missing. Refusing "
                "to construct Futures Demo preflight client."
            )
        if not api_secret:
            raise BinanceFuturesDemoMissingCredentials(
                "BINANCE_FUTURES_DEMO_API_SECRET is empty or missing. "
                "Refusing to construct Futures Demo preflight client."
            )
        base_url = os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", _DEFAULT_BASE_URL)
        return cls(api_key=api_key, api_secret=api_secret, base_url=base_url)

    def __repr__(self) -> str:
        # Never reference _api_secret in repr/str. api_key half is
        # fingerprinted to avoid log exposure of credentials.
        return (
            f"<FuturesDemoPreflightClient base_url={self._base_url!r} "
            f"api_key={_redact_api_key(self._api_key)!r}>"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def preflight_account(self) -> FuturesDemoPreflightResult:
        """Call ``GET /fapi/v1/account`` and return a redacted summary.

        Side effects: ONE signed HTTP GET against
        ``demo-fapi.binance.com``. No DB writes, no ledger writes, no
        order placement, no scheduler activation. The httpx transport
        refuses any non-Futures-Demo host even if the env was
        misconfigured.

        Raises:
          * ``BinanceFuturesDemoUnsupportedAuth`` if the server response
            indicates HMAC signing is rejected (codes -2014, -2008, -1022).
          * ``httpx.HTTPStatusError`` for other non-2xx responses (caller
            decides whether to log + exit non-zero).
        """
        signed = _sign_request_params(
            params={"recvWindow": BINANCE_FUTURES_DEMO_RECV_WINDOW_MS},
            api_secret=self._api_secret,
        )
        # Path MUST stay /fapi/v1/account. httpx joins base_url + path
        # without /fapi/fapi/v1 duplication because base_url has no path
        # component beyond the host.
        response = await self._client.get(_ACCOUNT_PATH, params=signed)
        if response.status_code >= 400:
            self._raise_for_auth_or_status(response)
        payload = response.json()
        summary = _summarize_account(payload)
        return FuturesDemoPreflightResult(
            source="futures_demo",
            venue="binance",
            product="usdm_futures",
            base_url=self._base_url,
            api_key_fingerprint=_redact_api_key(self._api_key),
            account_can_trade=summary["can_trade"],
            account_can_deposit=summary["can_deposit"],
            account_can_withdraw=summary["can_withdraw"],
            account_type=summary["account_type"],
            assets_nonzero_count=summary["assets_nonzero_count"],
            positions_nonzero_count=summary["positions_nonzero_count"],
        )

    def _raise_for_auth_or_status(self, response: httpx.Response) -> None:
        """Map Binance HMAC-rejection codes to UnsupportedAuth; otherwise raise.

        Binance returns JSON ``{"code": <int>, "msg": "..."}`` on signed
        endpoint errors. If the code matches one of the known HMAC
        rejection codes, raise ``BinanceFuturesDemoUnsupportedAuth`` so
        the operator reports the Ed25519/HMAC mismatch as a scope-expansion
        follow-up.

        The exception message redacts any echoed key/secret material:
        Binance error messages do not normally echo credentials, but we
        defensively strip anything that looks like the api_key or
        api_secret.
        """
        binance_code: int | None = None
        binance_msg: str = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                code_value = body.get("code")
                if isinstance(code_value, int):
                    binance_code = code_value
                msg_value = body.get("msg")
                if isinstance(msg_value, str):
                    binance_msg = msg_value
        except ValueError:
            pass
        if binance_code in _UNSUPPORTED_AUTH_BINANCE_CODES:
            redacted_msg = self._redact_credential_echoes(binance_msg)
            raise BinanceFuturesDemoUnsupportedAuth(
                f"Futures Demo server rejected HMAC-SHA256 signing with code "
                f"{binance_code} ({redacted_msg!r}). If your Futures Demo "
                "credentials are Ed25519, this is a scope expansion: do not "
                "patch a signer fallback in this PR. Report as ROB-298 PR 2 "
                "follow-up."
            )
        response.raise_for_status()

    def _redact_credential_echoes(self, message: str) -> str:
        """Strip any echoed api_key/api_secret bytes from ``message``."""
        redacted = message
        if self._api_key and self._api_key in redacted:
            redacted = redacted.replace(self._api_key, "<redacted-api-key>")
        if self._api_secret and self._api_secret in redacted:
            redacted = redacted.replace(self._api_secret, "<redacted-api-secret>")
        return redacted
