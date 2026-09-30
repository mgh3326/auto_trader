"""#1093 — KIS account credentials must never reach error/log/tool text.

Regression tests for the kis_mock account-number leak: every KIS raise path
that previously embedded the raw configured account value must emit only the
fixed ``[MASKED]`` placeholder, and HTTP errors raised inside the KIS client
must not carry the request URL (which contains ``CANO``/``ACNT_PRDT_CD`` query
parameters on GET calls) or the response body snippet.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.core.config import settings
from app.services.brokers.kis.base import mask_account_identifier
from app.services.brokers.kis.client import KISClient

# Malformed account values in every shape the parser can receive: contiguous,
# hyphenated, multi-hyphenated, and non-digit. None of their digits may appear
# in the raised message.
BAD_ACCOUNTS = [
    "1234",
    "123456789",
    "12345678-0",
    "1234-56",
    "12-34-56-78",
    "abcde",
    "507-1",
]

ERROR_PREFIX = "계좌번호 형식이 올바르지 않습니다"


def _assert_masked(exc_text: str, bad_value: str) -> None:
    assert ERROR_PREFIX in exc_text
    assert "[MASKED]" in exc_text
    # Neither the stored form nor its de-hyphenated (concatenated) form may
    # appear — masking cannot be bypassed by reformatting the same value.
    assert bad_value not in exc_text
    assert bad_value.replace("-", "") not in exc_text


class TestMaskAccountIdentifier:
    @pytest.mark.parametrize("value", ["12345678-01", "5071234501", "x"])
    def test_nonempty_value_returns_fixed_placeholder(self, value: str):
        masked = mask_account_identifier(value)
        assert masked == "[MASKED]"
        assert value not in masked
        assert value.replace("-", "") not in masked

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_value_returns_empty_string(self, value):
        assert mask_account_identifier(value) == ""


class TestResolveAccountPartsMasking:
    """Canonical parser path (account.py::_resolve_account_parts)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", BAD_ACCOUNTS)
    async def test_live_account_error_is_masked(self, bad: str, monkeypatch):
        monkeypatch.setattr(settings, "kis_account_no", bad)
        client = KISClient()
        with patch.object(client, "_ensure_token"):
            with pytest.raises(ValueError) as exc_info:
                await client.fetch_my_stocks(is_mock=False)
        _assert_masked(str(exc_info.value), bad)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", BAD_ACCOUNTS)
    async def test_mock_account_error_is_masked(self, bad: str, monkeypatch):
        # The mock client resolves its account from KIS_MOCK_ACCOUNT_NO via the
        # mock settings view — the exact leak reported in #1093.
        monkeypatch.setattr(settings, "kis_mock_account_no", bad)
        client = KISClient(is_mock=True)
        with patch.object(client, "_ensure_token"):
            with pytest.raises(ValueError) as exc_info:
                await client.inquire_mock_overseas_buyable_amount()
        _assert_masked(str(exc_info.value), bad)

    def test_valid_hyphenated_and_contiguous_forms_unchanged(self, monkeypatch):
        client = KISClient()
        for value, expected in [
            ("12345678-01", ("12345678", "01")),
            ("1234567801", ("12345678", "01")),
        ]:
            monkeypatch.setattr(settings, "kis_account_no", value)
            assert client._account._resolve_account_parts() == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, ""])
    async def test_missing_account_message_has_no_value(self, bad, monkeypatch):
        monkeypatch.setattr(settings, "kis_account_no", bad)
        client = KISClient()
        with patch.object(client, "_ensure_token"):
            with pytest.raises(ValueError) as exc_info:
                await client.fetch_my_stocks(is_mock=False)
        assert "환경변수가 설정되지 않았습니다" in str(exc_info.value)


def _domestic_calls(client: KISClient):
    return {
        "inquire_korea_orders": lambda: client.inquire_korea_orders(is_mock=False),
        "order_korea_stock": lambda: client.order_korea_stock(
            "005930", "buy", 1, 70000, is_mock=False
        ),
        "cancel_korea_order": lambda: client.cancel_korea_order(
            "0000123456", "005930", 1, 70000, "buy", is_mock=False
        ),
        "inquire_daily_order_domestic": lambda: client.inquire_daily_order_domestic(
            "20260101", "20260102", is_mock=False
        ),
        "modify_korea_order": lambda: client.modify_korea_order(
            "0000123456", "005930", 1, 71000, is_mock=False
        ),
    }


def _overseas_calls(client: KISClient):
    return {
        "order_overseas_stock": lambda: client.order_overseas_stock(
            "AAPL", "NASD", "buy", 1, 150.0, is_mock=False
        ),
        "inquire_overseas_orders": lambda: client.inquire_overseas_orders(
            "NASD", is_mock=False
        ),
        "cancel_overseas_order": lambda: client.cancel_overseas_order(
            "0000123456", "AAPL", "NASD", 1, is_mock=False
        ),
        "inquire_daily_order_overseas": lambda: client.inquire_daily_order_overseas(
            "20260101", "20260102", is_mock=False
        ),
        "modify_overseas_order": lambda: client.modify_overseas_order(
            "0000123456", "AAPL", "NASD", 1, 150.0, is_mock=False
        ),
    }


class TestOrderPathMasking:
    """Every inline account-format raise in the order sub-clients is masked."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["12345678-0", "507"])
    @pytest.mark.parametrize(
        "call_name",
        [
            "inquire_korea_orders",
            "order_korea_stock",
            "cancel_korea_order",
            "inquire_daily_order_domestic",
            "modify_korea_order",
        ],
    )
    async def test_domestic_raise_path_masks_account(
        self, bad: str, call_name: str, monkeypatch
    ):
        monkeypatch.setattr(settings, "kis_account_no", bad)
        client = KISClient()
        with patch.object(client, "_ensure_token"):
            with pytest.raises(ValueError) as exc_info:
                await _domestic_calls(client)[call_name]()
        _assert_masked(str(exc_info.value), bad)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["12345678-0", "507"])
    @pytest.mark.parametrize(
        "call_name",
        [
            "order_overseas_stock",
            "inquire_overseas_orders",
            "cancel_overseas_order",
            "inquire_daily_order_overseas",
            "modify_overseas_order",
        ],
    )
    async def test_overseas_raise_path_masks_account(
        self, bad: str, call_name: str, monkeypatch
    ):
        monkeypatch.setattr(settings, "kis_account_no", bad)
        client = KISClient()
        with patch.object(client, "_ensure_token"):
            with pytest.raises(ValueError) as exc_info:
                await _overseas_calls(client)[call_name]()
        _assert_masked(str(exc_info.value), bad)

    @pytest.mark.asyncio
    async def test_mock_order_history_path_masks_mock_account(self, monkeypatch):
        """The reported #1093 path: kis_mock order history via a mock client."""
        monkeypatch.setattr(settings, "kis_mock_account_no", "50712345")
        client = KISClient(is_mock=True)
        with patch.object(client, "_ensure_token"):
            with pytest.raises(ValueError) as exc_info:
                await client.inquire_daily_order_domestic(
                    "20260101", "20260102", is_mock=True
                )
        _assert_masked(str(exc_info.value), "50712345")

    @pytest.mark.asyncio
    async def test_mcp_tool_error_payload_is_masked(self, monkeypatch):
        """End-to-end: get_order_history_impl stores str(e) in errors[].error.

        Reproduces the reported leak — the configured mock account value must
        not appear verbatim in the tool-facing error payload.
        """
        from app.mcp_server.tooling import orders_history

        async def _noop_token(self):
            return None

        async def _no_shadow_orders(**kwargs):
            return []

        monkeypatch.setattr(settings, "kis_mock_account_no", "50712345")
        monkeypatch.setattr(KISClient, "_ensure_token", _noop_token)
        # Keep the broker fetcher path deterministic: the shadow pending
        # ledger needs a DB that may not exist under a throwaway env.
        monkeypatch.setattr(
            orders_history,
            "_list_kis_mock_shadow_pending_orders",
            _no_shadow_orders,
        )

        result = await orders_history.get_order_history_impl(
            symbol="005930", market="kr", is_mock=True
        )

        kr_errors = [e for e in result["errors"] if e.get("market") == "equity_kr"]
        assert kr_errors, "expected the masked ValueError in errors[]"
        joined = " ".join(e["error"] for e in kr_errors)
        assert "[MASKED]" in joined
        assert "50712345" not in joined


class TestHttpStatusErrorSanitization:
    """httpx raise_for_status embeds the request URL (CANO/ACNT_PRDT_CD query
    params) plus a response-body snippet — neither may surface."""

    @staticmethod
    def _response(status: int) -> httpx.Response:
        request = httpx.Request(
            "GET",
            "https://openapivts.koreainvestment.com:29443"
            "/uapi/domestic-stock/v1/trading/inquire-balance"
            "?CANO=12345678&ACNT_PRDT_CD=01",
        )
        return httpx.Response(
            status,
            request=request,
            content=b'{"msg_cd":"EGW00121","msg1":"bad account 12345678"}',
        )

    @pytest.mark.asyncio
    async def test_http_error_message_has_no_url_or_body(self, monkeypatch):
        monkeypatch.setattr(settings, "kis_account_no", "12345678-01")
        client = KISClient()
        mock_response = self._response(403)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client._parse_kis_response(mock_response, "fetch_test")
        text = str(exc_info.value)
        assert "12345678" not in text
        assert "CANO" not in text
        assert "ACNT_PRDT_CD" not in text
        assert "for url" not in text
        assert "EGW00121" not in text
        assert "fetch_test" in text
        # The retry/inspection contract is preserved: same type, same response.
        assert exc_info.value.response is mock_response
        assert exc_info.value.response.status_code == 403

    def test_http_error_still_raises_httpstatuserror(self):
        client = KISClient()
        with pytest.raises(httpx.HTTPStatusError):
            client._parse_kis_response(self._response(404), "fetch_test")

    def test_http_error_exception_chain_is_suppressed(self):
        """logging.exception / traceback / Sentry walk __cause__ + __context__.

        If the original HTTPStatusError (whose message carries the request URL
        with CANO/ACNT_PRDT_CD) survives in the chain, formatted tracebacks and
        chained-exception handlers still leak the account fields.
        """
        import traceback

        client = KISClient()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client._parse_kis_response(self._response(403), "fetch_test")
        exc = exc_info.value
        assert exc.__cause__ is None
        assert exc.__suppress_context__ is True
        rendered = "".join(traceback.format_exception(exc))
        assert "12345678" not in rendered
        assert "CANO" not in rendered
        assert "ACNT_PRDT_CD" not in rendered
        assert "for url" not in rendered
