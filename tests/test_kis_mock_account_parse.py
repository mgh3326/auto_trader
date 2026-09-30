"""#1104 — one shared account parser for every kis_mock path.

``KIS_MOCK_ACCOUNT_NO`` may hold a bare 8-digit account number (no product
code, no hyphen). The shared parser in
``app/services/brokers/kis/account_no.py`` is the only place that resolves it:
8 digits -> implicit product code ``01`` (the same default the working
broker-edge mock order path relies on), 10 digits and ``8-2`` unchanged, and
anything else rejected with the masked ``[MASKED]`` error from #1093. Live
parsing keeps the historical grammar exactly.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.brokers.kis.account_no import (
    KIS_MOCK_DEFAULT_PRODUCT_CODE,
    mask_account_identifier,
    parse_kis_account_parts,
    resolve_kis_account_parts,
)
from app.services.brokers.kis.client import KISClient

_ERROR_PREFIX = "계좌번호 형식이 올바르지 않습니다"


def _assert_masked(exc_text: str, bad_value: str) -> None:
    assert _ERROR_PREFIX in exc_text
    assert "[MASKED]" in exc_text
    assert bad_value not in exc_text
    assert bad_value.replace("-", "") not in exc_text


class TestParseKisAccountPartsMock:
    """Mock grammar: 8 digits + implicit code, 10 digits, or 8-2."""

    def test_bare_eight_digits_applies_default_product_code(self):
        assert parse_kis_account_parts("12345678", is_mock=True) == (
            "12345678",
            KIS_MOCK_DEFAULT_PRODUCT_CODE,
        )
        assert KIS_MOCK_DEFAULT_PRODUCT_CODE == "01"

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("1234567801", ("12345678", "01")),
            ("1234567899", ("12345678", "99")),
            ("12345678-34", ("12345678", "34")),
            ("00000000-02", ("00000000", "02")),
            (" 12345678-07 ", ("12345678", "07")),
        ],
    )
    def test_qualified_forms_unchanged(self, value: str, expected: tuple[str, str]):
        assert parse_kis_account_parts(value, is_mock=True) == expected

    def test_no_double_apply(self):
        """A qualified product code is never rewritten by the default.

        "12345678-34" must keep "34" (not "01"), and an 8-digit input yields
        exactly "01" — never a re-suffixed "0101".
        """
        assert parse_kis_account_parts("12345678-34", is_mock=True)[1] == "34"
        assert parse_kis_account_parts("1234567899", is_mock=True)[1] == "99"
        assert parse_kis_account_parts("12345678", is_mock=True)[1] == "01"

    @pytest.mark.parametrize(
        "bad",
        [
            "1234567",  # 7 digits
            "123456789",  # 9 digits
            "12345678901",  # 11 digits
            "12345678-1",  # 8-1
            "1234567-801",  # 7-3
            "1234-5678-01",  # multi-hyphen
            "12345678-010",  # 8-3
            "12-34567801",  # hyphen elsewhere
            "abcdefgh",
            "1234567a",
            "12345678-",  # trailing hyphen
            "-12345678",  # leading hyphen
            "1234 5678",
        ],
    )
    def test_malformed_values_rejected_masked(self, bad: str):
        with pytest.raises(ValueError) as exc_info:
            parse_kis_account_parts(bad, is_mock=True)
        _assert_masked(str(exc_info.value), bad)


class TestParseKisAccountPartsLive:
    """Live grammar is the historical one — unchanged."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("1234567890", ("12345678", "90")),
            ("12345678-01", ("12345678", "01")),
            ("12-3456-7801", ("12345678", "01")),
            ("123456789012", ("12345678", "90")),
        ],
    )
    def test_live_accepts_as_before(self, value: str, expected: tuple[str, str]):
        # Legacy live behavior: strip all hyphens, require >= 10 chars, take
        # [0:8] / [8:10] — including over-long values.
        assert parse_kis_account_parts(value, is_mock=False) == expected

    def test_live_eight_digits_still_rejected(self):
        """The mock-only leniency must not leak into live parsing."""
        with pytest.raises(ValueError) as exc_info:
            parse_kis_account_parts("12345678", is_mock=False)
        _assert_masked(str(exc_info.value), "12345678")

    def test_live_error_is_masked(self):
        with pytest.raises(ValueError) as exc_info:
            parse_kis_account_parts("1234-56", is_mock=False)
        _assert_masked(str(exc_info.value), "1234-56")


class TestMissingAccount:
    def test_missing_mock_env_names_mock_key(self):
        with pytest.raises(ValueError) as exc_info:
            parse_kis_account_parts(None, is_mock=True)
        assert "KIS_MOCK_ACCOUNT_NO" in str(exc_info.value)

    def test_missing_live_env_names_live_key(self):
        with pytest.raises(ValueError) as exc_info:
            parse_kis_account_parts(None, is_mock=False)
        assert "KIS_ACCOUNT_NO" in str(exc_info.value)
        assert "KIS_MOCK_ACCOUNT_NO" not in str(exc_info.value)

    def test_empty_value_rejected(self):
        for empty in ("", "   "):
            with pytest.raises(ValueError):
                parse_kis_account_parts(empty, is_mock=True)
            with pytest.raises(ValueError):
                parse_kis_account_parts(empty, is_mock=False)


class TestMaskAccountIdentifier:
    def test_placeholder(self):
        assert mask_account_identifier("12345678-01") == "[MASKED]"
        assert mask_account_identifier("x") == "[MASKED]"
        assert mask_account_identifier("") == ""
        assert mask_account_identifier(None) == ""


class TestSettingsViewBinding:
    """Mock-vs-live is decided by the account binding, not the TR flag."""

    def test_mock_client_view_scope(self):
        assert KISClient(is_mock=True)._settings.account_scope == "kis_mock"

    def test_live_client_view_scope(self):
        assert KISClient()._settings.account_scope == "kis_live"

    def test_b0x_mock_settings_view_scope(self):
        from app.services.kis_mock_settings_view import KISMockSettingsView

        assert KISMockSettingsView(settings).account_scope == "kis_mock"

    def test_invest_home_mock_proxy_scope(self):
        from app.services.invest_home_readers import _KISMockSettingsProxy

        assert _KISMockSettingsProxy(settings).account_scope == "kis_mock"

    def test_plain_settings_has_no_scope(self, monkeypatch):
        # The live settings object must not gain mock leniency.
        monkeypatch.setattr(settings, "kis_account_no", "12345678")
        with pytest.raises(ValueError):
            resolve_kis_account_parts(settings)


@pytest.fixture
def _kis_mock_wire_authority(monkeypatch):
    """Stand in for the PostgreSQL writer lease; tests routing, not authority."""
    import app.services.kis_mock_runner.singleton as singleton

    @contextlib.asynccontextmanager
    async def _lease(**kwargs):
        token = singleton._ACTIVE_WRITER_LEASE.set(
            singleton._WriterAuthority(
                account_mode=singleton.ACCOUNT_MODE,
                advisory_keys=(singleton.kis_mock_legacy_advisory_key(),),
                lease=object(),
            )
        )
        try:
            yield
        finally:
            singleton._ACTIVE_WRITER_LEASE.reset(token)

    monkeypatch.setattr(singleton, "enforce_kis_mock_mutation_writer", _lease)


def _capture_request():
    captured: dict = {}

    async def fake_request(
        method, url, *, headers, params=None, json_body=None, **kwargs
    ):
        captured["params"] = params
        captured["json_body"] = json_body
        return {
            "rt_cd": "0",
            "output": {"ODNO": "1", "ORD_TMD": "100000", "ord_psbl_frcr_amt": "100.0"},
            "output1": [],
        }

    return captured, fake_request


def _wire_account(captured: dict) -> tuple[str | None, str | None]:
    payload = captured.get("json_body") or captured.get("params") or {}
    return payload.get("CANO"), payload.get("ACNT_PRDT_CD")


class TestEightDigitMockTupleParity:
    """AC3: the 8-digit mock value yields one (CANO, ACNT_PRDT_CD) tuple on
    both the order path and the order-history path."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_kis_mock_wire_authority")
    async def test_mock_paths_agree_on_wire_account(self, monkeypatch):
        monkeypatch.setattr(settings, "kis_mock_account_no", "12345678")
        client = KISClient(is_mock=True)
        monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))

        sent: dict[str, tuple[str | None, str | None]] = {}

        captured, fake = _capture_request()
        monkeypatch.setattr(client, "_request_with_rate_limit", fake)
        await client.order_korea_stock(
            stock_code="005930",
            order_type="buy",
            quantity=1,
            price=70000,
            is_mock=True,
        )
        sent["order_korea_stock"] = _wire_account(captured)

        captured, fake = _capture_request()
        monkeypatch.setattr(client, "_request_with_rate_limit", fake)
        await client.inquire_daily_order_domestic(
            start_date="20260101", end_date="20260102", is_mock=True
        )
        sent["inquire_daily_order_domestic"] = _wire_account(captured)

        captured, fake = _capture_request()
        monkeypatch.setattr(client, "_request_with_rate_limit", fake)
        await client.inquire_daily_order_overseas(
            start_date="20260101",
            end_date="20260102",
            symbol="AAPL",
            exchange_code="NASD",
            is_mock=True,
        )
        sent["inquire_daily_order_overseas"] = _wire_account(captured)

        captured, fake = _capture_request()
        monkeypatch.setattr(client, "_request_with_rate_limit", fake)
        await client.inquire_mock_overseas_buyable_amount()
        sent["inquire_mock_overseas_buyable_amount"] = _wire_account(captured)

        assert sent == {
            "order_korea_stock": ("12345678", "01"),
            "inquire_daily_order_domestic": ("12345678", "01"),
            "inquire_daily_order_overseas": ("12345678", "01"),
            "inquire_mock_overseas_buyable_amount": ("12345678", "01"),
        }

    @pytest.mark.asyncio
    async def test_ten_digit_and_hyphenated_forms_unchanged(self, monkeypatch):
        for configured, expected in (
            ("1234567890", ("12345678", "90")),
            ("12345678-34", ("12345678", "34")),
        ):
            monkeypatch.setattr(settings, "kis_mock_account_no", configured)
            client = KISClient(is_mock=True)
            monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
            captured, fake = _capture_request()
            monkeypatch.setattr(client, "_request_with_rate_limit", fake)
            await client.inquire_daily_order_domestic(
                start_date="20260101", end_date="20260102", is_mock=True
            )
            assert _wire_account(captured) == expected

    @pytest.mark.asyncio
    async def test_invalid_mock_account_masked_no_request(self, monkeypatch):
        bad = "123456789"
        monkeypatch.setattr(settings, "kis_mock_account_no", bad)
        client = KISClient(is_mock=True)
        monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
        request_mock = AsyncMock(
            side_effect=AssertionError("must not send on parse failure")
        )
        monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
        with pytest.raises(ValueError) as exc_info:
            await client.inquire_daily_order_domestic(
                start_date="20260101", end_date="20260102", is_mock=True
            )
        _assert_masked(str(exc_info.value), bad)
        request_mock.assert_not_called()


class TestLivePathUnchanged:
    @pytest.mark.asyncio
    async def test_live_client_rejects_eight_digit_account(self, monkeypatch):
        monkeypatch.setattr(settings, "kis_account_no", "12345678")
        client = KISClient()
        monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
        request_mock = AsyncMock(
            side_effect=AssertionError("must not send on parse failure")
        )
        monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
        with pytest.raises(ValueError) as exc_info:
            await client.inquire_daily_order_domestic(
                start_date="20260101", end_date="20260102", is_mock=False
            )
        _assert_masked(str(exc_info.value), "12345678")
        request_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_client_ten_digit_unchanged(self, monkeypatch):
        monkeypatch.setattr(settings, "kis_account_no", "12345678-90")
        client = KISClient()
        monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
        captured, fake = _capture_request()
        monkeypatch.setattr(client, "_request_with_rate_limit", fake)
        await client.inquire_daily_order_domestic(
            start_date="20260101", end_date="20260102", is_mock=False
        )
        assert _wire_account(captured) == ("12345678", "90")


class TestFingerprintCanonicalization:
    def test_all_spellings_share_one_identity(self):
        from app.services.kis_mock_runner.singleton import (
            kis_mock_account_fingerprint,
        )

        fp_8 = kis_mock_account_fingerprint(app_key="key", account_no="12345678")
        fp_hyph = kis_mock_account_fingerprint(app_key="key", account_no="12345678-01")
        fp_10 = kis_mock_account_fingerprint(app_key="key", account_no="1234567801")
        assert fp_8 == fp_hyph == fp_10
        assert fp_8.startswith("kismock:v1:")

    def test_fingerprint_rejects_malformed(self):
        import app.services.kis_mock_runner.singleton as singleton

        with pytest.raises(singleton.KISMockSendBoundaryRejected):
            singleton.kis_mock_account_fingerprint(app_key="key", account_no="123")
        with pytest.raises(singleton.KISMockSendBoundaryRejected):
            singleton.kis_mock_account_fingerprint(app_key="", account_no="1234567801")
