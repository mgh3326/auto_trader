"""hk 5342 — KIS 해외 체결조회가 transient 재시도 소진 뒤 부분 목록을
완전한 목록처럼 돌려주지 않는다.

증상(운영 2026-11-19 10:40 KST, hk 3794 run kis-us 단계): 한 페이지가 재시도
가능한 msg_cd(SYDB0050)로 RETRYABLE_MAX_ATTEMPTS를 소진하면 이미 누적된 행만
돌려주고 정상 종료와 구분되지 않았다 — 같은 함수가 max_pages 도달은 이미
RuntimeError로 실패시키는데 두 불완전 경로의 계약이 서로 달랐다. 부분 목록을
증거로 읽는 reconcile/evidence 호출자는 주문 부재를 잘못 단정할 수 있었다.

계약:
* 기본값은 fail-closed — 소진 시 RuntimeError(truncated와 같은 종류), 메시지는
  page·누적 건수·msg_cd를 담고 브로커 원문 페이로드는 담지 않는다.
* 표시 전용 호출자만 ``allow_partial_on_transient_exhausted=True``로 누적 행을
  받는다.
* 시도 예산 안에서 회복되는 재시도 동작은 바뀌지 않는다.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_overseas_client():
    from app.services.brokers.kis.overseas_orders import OverseasOrderClient

    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()
    token_manager = MagicMock()
    token_manager.clear_token = AsyncMock()
    parent._token_manager = token_manager
    parent._kis_url = lambda path: f"https://host{path}"
    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings
    return OverseasOrderClient(parent), parent


def _page(rows: list[dict], nk: str) -> dict:
    return {
        "rt_cd": "0",
        "output1": rows,
        "ctx_area_fk200": nk,
        "ctx_area_nk200": nk,
    }


def _retryable_error() -> dict:
    return {
        "rt_cd": "1",
        "msg_cd": "SYDB0050",
        "msg1": "transient broker condition",
    }


_FIRST_PAGE_ROWS = [{"odno": f"R{i:03d}", "pdno": "AAPL"} for i in range(20)]


@pytest.fixture
def no_sleep(monkeypatch):
    sleep_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.brokers.kis.overseas_orders.asyncio.sleep", sleep_mock
    )
    return sleep_mock


@pytest.mark.unit
class TestTransientExhaustedContract:
    @pytest.mark.asyncio
    async def test_exhausted_retries_raise_instead_of_returning_partial(self, no_sleep):
        """(a) 누적 행이 있어도 기본값은 실패한다 — 부분 목록은 완전한 목록이 아니다."""
        instance, parent = _make_overseas_client()
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[_page(_FIRST_PAGE_ROWS, "NK1")] + [_retryable_error()] * 10
        )

        with pytest.raises(RuntimeError) as excinfo:
            await instance.inquire_daily_order_overseas(
                start_date="20261118", end_date="20261119"
            )

        message = str(excinfo.value)
        assert "incomplete" in message
        assert "page=2" in message
        assert "accumulated=20" in message
        assert "SYDB0050" in message
        # 브로커 원문 메시지(msg1)·페이로드는 메시지에 싣지 않는다.
        assert "transient broker condition" not in message
        # 시도 예산: 최초 1회 + RETRYABLE_MAX_ATTEMPTS회 재시도 뒤 실패.
        assert parent._request_with_rate_limit.call_count == 4

    @pytest.mark.asyncio
    async def test_opt_in_returns_accumulated_rows_and_logs_warning(self, caplog):
        """(b) 표시 전용 opt-in: 누적 행을 반환하고 경고를 남긴다."""
        instance, parent = _make_overseas_client()
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[_page(_FIRST_PAGE_ROWS, "NK1")] + [_retryable_error()] * 10
        )

        with caplog.at_level("WARNING"):
            result = await instance.inquire_daily_order_overseas(
                start_date="20261118",
                end_date="20261119",
                allow_partial_on_transient_exhausted=True,
            )

        assert result == _FIRST_PAGE_ROWS
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("누적 20건을 반환" in w and "page=2" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_retryable_error_recovering_within_budget_returns_full_list(
        self, no_sleep
    ):
        """(c) 예산 안에서 회복하면 전체 목록을 돌려주고 실패하지 않는다."""
        instance, parent = _make_overseas_client()
        second_page_rows = [{"odno": "R020", "pdno": "MSFT"}]
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[
                _page(_FIRST_PAGE_ROWS, "NK1"),
                _retryable_error(),
                _page(second_page_rows, ""),
            ]
        )

        result = await instance.inquire_daily_order_overseas(
            start_date="20261118", end_date="20261119"
        )

        assert result == _FIRST_PAGE_ROWS + second_page_rows
        assert parent._request_with_rate_limit.call_count == 3
        # 재시도 backoff 1회 + 페이지 사이 기본 지연(inter_page_delay=0.1) 1회.
        assert no_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_max_pages_truncation_still_raises(self, no_sleep):
        """(d) max_pages 잘림 계약 불변."""
        instance, parent = _make_overseas_client()
        parent._request_with_rate_limit = AsyncMock(
            side_effect=[
                _page([{"odno": f"R{i:03d}", "pdno": "AAPL"}], f"NK{i}")
                for i in range(10)
            ]
        )

        with pytest.raises(RuntimeError, match="truncated"):
            await instance.inquire_daily_order_overseas(
                start_date="20261118", end_date="20261119", max_pages=3
            )


@pytest.mark.unit
class TestCallerOptInDecision:
    """opt-in은 표시 경로만 — evidence/주문 조회 호출자는 기본값을 유지한다."""

    @pytest.mark.asyncio
    async def test_order_history_display_opts_in(self, monkeypatch):
        import app.mcp_server.tooling.orders_history as oh

        captured: dict = {}

        async def fake_overseas(*args, **kwargs):
            captured.update(kwargs)
            return []

        fake_client = MagicMock()
        fake_client.inquire_daily_order_overseas = fake_overseas
        fake_client.inquire_overseas_orders = AsyncMock(return_value=[])
        monkeypatch.setattr(oh, "_create_kis_client", lambda *, is_mock: fake_client)
        monkeypatch.setattr(
            oh, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
        )

        await oh._fetch_us_orders("AAPL", "filled", effective_days=30, is_mock=False)

        assert captured.get("allow_partial_on_transient_exhausted") is True

    @pytest.mark.asyncio
    async def test_filled_orders_service_does_not_opt_in(self, monkeypatch):
        """execution_ledger reconcile이 쓰는 체결 조회 경로는 기본(fail-closed)."""
        from app.services import filled_orders_service as svc

        fake_kis = MagicMock()
        fake_kis.inquire_daily_order_overseas = AsyncMock(return_value=[])
        monkeypatch.setattr(svc, "KISClient", lambda: fake_kis)

        await svc._fetch_kis_overseas_filled(days=7)

        kwargs = fake_kis.inquire_daily_order_overseas.await_args.kwargs
        assert not kwargs.get("allow_partial_on_transient_exhausted")

    @pytest.mark.asyncio
    async def test_modify_cancel_history_lookup_does_not_opt_in(self):
        from app.mcp_server.tooling.orders_modify_cancel import (
            _find_us_order_in_recent_history,
        )

        fake_kis = MagicMock()
        fake_kis.inquire_daily_order_overseas = AsyncMock(return_value=[])

        await _find_us_order_in_recent_history(fake_kis, "0000123456", "AAPL", ["NASD"])

        kwargs = fake_kis.inquire_daily_order_overseas.await_args.kwargs
        assert not kwargs.get("allow_partial_on_transient_exhausted")
