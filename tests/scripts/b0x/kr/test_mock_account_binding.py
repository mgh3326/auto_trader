"""Regression guard — the KR lane's read client is bound to the *mock* account.

The defect these tests exist for (2026-08-10, first RTH-reaching KR cycle):
``ReadOnlyKISMockDomesticClient`` subclassed ``BaseKISClient`` and called
``AccountClient(..., is_mock=True)``, but ``is_mock`` selects the **TR id and
nothing else**. Host, app key/secret, account number and OAuth token were all
inherited from ``BaseKISClient``'s live defaults, so the client sent the mock
TR ``VTTC8434R`` to the **live** host with **live** credentials and KIS replied
``EGW02005 실전투자 TR 이 아닙니다``.

The lane's acceptance run the previous day (a Sunday) passed only because the
RTH gate short-circuited before any account I/O — the call was never reached.
These tests reach it without a network: every assertion below is about how the
client resolves its host/credentials/token *before* a socket is opened, so
they fail on a workstation, in CI, and on a closed market alike.

Nothing here performs network or broker I/O.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from scripts.b0x.kr import mock as kr_mock

pytestmark = pytest.mark.unit

LIVE_HOST_URL = "https://openapi.koreainvestment.com:9443"
MOCK_HOST_URL = "https://openapivts.koreainvestment.com:29443"


@pytest.fixture
def mock_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the process a complete, obviously-fake kis_mock credential set."""

    monkeypatch.setattr(settings, "kis_mock_app_key", "MOCK-APP-KEY", raising=False)
    monkeypatch.setattr(
        settings, "kis_mock_app_secret", "MOCK-APP-SECRET", raising=False
    )
    monkeypatch.setattr(settings, "kis_mock_account_no", "9999999999", raising=False)
    monkeypatch.setattr(settings, "kis_mock_base_url", MOCK_HOST_URL, raising=False)
    monkeypatch.setattr(settings, "kis_mock_access_token", None, raising=False)
    # Live values that must never be picked up by this client.
    monkeypatch.setattr(settings, "kis_app_key", "LIVE-APP-KEY", raising=False)
    monkeypatch.setattr(settings, "kis_app_secret", "LIVE-APP-SECRET", raising=False)
    monkeypatch.setattr(settings, "kis_account_no", "1111111111", raising=False)
    monkeypatch.setattr(settings, "kis_base_url", LIVE_HOST_URL, raising=False)


# ---------------------------------------------------------------------------
# The four bindings that ``is_mock=True`` does not perform on its own.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_credentials")
def test_every_url_the_client_builds_points_at_the_mock_host() -> None:
    client = kr_mock.ReadOnlyKISMockDomesticClient()

    # The balance read and the OAuth token POST both go through _kis_url.
    for path in ("/uapi/domestic-stock/v1/trading/inquire-balance", "/oauth2/token"):
        assert client._kis_url(path).startswith(MOCK_HOST_URL), path
        assert "openapi.koreainvestment.com" not in client._kis_url(path)


@pytest.mark.usefixtures("mock_credentials")
def test_credentials_and_account_number_are_the_mock_set_not_the_live_one() -> None:
    client = kr_mock.ReadOnlyKISMockDomesticClient()

    assert client._settings.kis_app_key == "MOCK-APP-KEY"
    assert client._settings.kis_app_secret == "MOCK-APP-SECRET"
    assert client._settings.kis_account_no == "9999999999"
    # _hdr_base is built during __init__ — the header actually sent must carry
    # the mock key, which is why the settings view is installed before super().
    assert client._hdr_base["appkey"] == "MOCK-APP-KEY"
    assert client._hdr_base["appsecret"] == "MOCK-APP-SECRET"


@pytest.mark.usefixtures("mock_credentials")
def test_the_token_slot_and_namespace_are_isolated_from_the_live_cache() -> None:
    client = kr_mock.ReadOnlyKISMockDomesticClient()

    # A token issued for the mock account must never land in the live slot.
    client._settings.kis_access_token = "MOCK-TOKEN"
    assert settings.kis_mock_access_token == "MOCK-TOKEN"
    assert settings.kis_access_token != "MOCK-TOKEN"

    from app.services.redis_token_manager import redis_token_manager

    assert client._token_manager is not redis_token_manager
    # Distinct Redis key space — a mock token cannot be served to a live client.
    assert client._token_manager._token_key.startswith("kis_mock:")
    assert client._token_manager._token_key != redis_token_manager._token_key


@pytest.mark.usefixtures("mock_credentials")
def test_the_client_is_flagged_as_mock_dispatch_for_the_vts_admit_gate() -> None:
    """ROB-892's distributed gate keys off this flag (and the mock host)."""

    client = kr_mock.ReadOnlyKISMockDomesticClient()
    assert client._is_mock_client is True


# ---------------------------------------------------------------------------
# Fail-closed: a non-mock host is refused before any socket is opened.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_credentials")
@pytest.mark.parametrize(
    "bad_base_url",
    [
        LIVE_HOST_URL,
        "https://openapi.koreainvestment.com:29443",  # live host, mock port
        "https://example.invalid",
        "",
    ],
    ids=["live", "live-host-mock-port", "foreign", "empty"],
)
def test_a_non_mock_host_is_refused_at_construction(
    monkeypatch: pytest.MonkeyPatch, bad_base_url: str
) -> None:
    monkeypatch.setattr(settings, "kis_mock_base_url", bad_base_url, raising=False)

    with pytest.raises(kr_mock.KrMockHostViolation):
        kr_mock.ReadOnlyKISMockDomesticClient()


@pytest.mark.usefixtures("mock_credentials")
def test_repointing_the_base_url_after_construction_still_cannot_reach_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host check is per-URL, not a one-off at construction."""

    client = kr_mock.ReadOnlyKISMockDomesticClient()
    monkeypatch.setattr(settings, "kis_mock_base_url", LIVE_HOST_URL, raising=False)

    with pytest.raises(kr_mock.KrMockHostViolation):
        client._kis_url("/uapi/domestic-stock/v1/trading/inquire-balance")


# ---------------------------------------------------------------------------
# Fail-closed: an unreadable cash figure is never rendered as zero.
# ---------------------------------------------------------------------------


class _CashOnlyClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def inquire_cash_balance(self) -> dict[str, Any]:
        return self._payload

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [{}, {"dnca_tot_amt": ""}, {"some_other_field": "1"}, None],
    ids=["no-rows", "blank-field", "unrelated-fields", "not-a-dict"],
)
async def test_a_response_with_no_cash_field_fails_closed_instead_of_reading_zero(
    raw: Any,
) -> None:
    # AccountClient coerces missing cash to 0.0, so the top-level figures look
    # like a legitimately empty account. Only ``raw`` reveals that the broker
    # reported nothing at all.
    client = _CashOnlyClient(
        {"dnca_tot_amt": 0.0, "stck_cash_ord_psbl_amt": 0.0, "raw": raw}
    )

    with pytest.raises(kr_mock.KrMockCashUnreadable):
        await kr_mock.read_fresh_truth(client)


@pytest.mark.asyncio
async def test_a_genuinely_empty_account_is_zero_and_not_an_error() -> None:
    """An echoed ``"0"`` is evidence, not absence — it must still read."""

    client = _CashOnlyClient(
        {
            "dnca_tot_amt": 0.0,
            "stck_cash_ord_psbl_amt": 0.0,
            "raw": {"dnca_tot_amt": "0", "stck_cash_ord_psbl_amt": "0"},
        }
    )

    fresh = await kr_mock.read_fresh_truth(client)
    assert fresh.cash == 0
    assert fresh.nav == 0


@pytest.mark.asyncio
async def test_zero_orderable_still_falls_back_to_the_echoed_deposit_total() -> None:
    """The lane's documented cash preference is unchanged by the new guard."""

    client = _CashOnlyClient(
        {
            "dnca_tot_amt": 500000.0,
            "stck_cash_ord_psbl_amt": 0.0,
            "raw": {"dnca_tot_amt": "500000", "stck_cash_ord_psbl_amt": "0"},
        }
    )

    fresh = await kr_mock.read_fresh_truth(client)
    assert fresh.cash == 500000
