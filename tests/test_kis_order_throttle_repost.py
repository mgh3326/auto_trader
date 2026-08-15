"""KIS gateway throttle response classification at the order transport layer.

Live evidence (2026-07-22 us-live session): two KIS overseas sells (BAC, IVV)
were answered ``EGW00201 초당 거래건수를 초과하였습니다`` and terminalized as
``rejected`` while five sibling sells on the same account succeeded. The
rejection is issued at the gateway, before the order engine — no order exists —
so a bounded re-POST is safe and is the difference between a filled trim and a
session-long dead proposal.

The low-level order clients never re-POST a response by themselves. They
surface only a typed candidate to the live execution boundary, which combines
it with the local idempotency reservation and ledger evidence. Every ambiguous
outcome remains fail-closed here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis.order_throttle import (
    MAX_THROTTLE_RESUBMITS,
    KISGatewayThrottleRejection,
    gateway_throttle_rejection_from_response,
    is_provider_throttle_reject,
    throttle_backoff_seconds,
)
from app.services.brokers.kis.send_outcome import OrderSendOutcomeTracker

_THROTTLE_BODY = {
    "rt_cd": "1",
    "msg_cd": "EGW00201",
    "msg1": "초당 거래건수를 초과하였습니다.",
}
_ACCEPTED_BODY = {
    "rt_cd": "0",
    "output": {"ODNO": "0030808418", "ORD_TMD": "233959"},
    "msg1": "정상처리 되었습니다.",
}


def _make_parent(responses):
    """Parent whose transport yields ``responses`` in order."""
    parent = MagicMock()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {}
    parent._kis_url = lambda path: f"https://host{path}"
    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings
    parent._request_with_rate_limit = AsyncMock(side_effect=list(responses))
    return parent


async def _normal_gateway_throttle(*_args, **kwargs):
    """Simulate the real normal-HTTP KIS gateway rejection shape."""
    tracker = kwargs["send_outcome"]
    tracker.mark_dispatched()
    tracker.mark_http_response(200)
    return _THROTTLE_BODY


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep the backoff contract but not its wall-clock cost."""
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        "app.services.brokers.kis.overseas_orders.asyncio.sleep", _fake_sleep
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.asyncio.sleep", _fake_sleep
    )
    return slept


@pytest.fixture(autouse=True)
def _no_nxt(monkeypatch):
    from app.services.brokers.kis import domestic_orders

    monkeypatch.setattr(
        domestic_orders, "is_nxt_eligible", AsyncMock(return_value=False)
    )


def _overseas(responses):
    from app.services.brokers.kis.overseas_orders import OverseasOrderClient

    parent = _make_parent(responses)
    return OverseasOrderClient(parent), parent


def _domestic(responses):
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = _make_parent(responses)
    return DomesticOrderClient(parent), parent


@pytest.mark.unit
class TestThrottleClassifier:
    def test_documented_codes_classify(self):
        assert is_provider_throttle_reject(
            "EGW00201", "초당 거래건수를 초과하였습니다."
        )
        assert is_provider_throttle_reject("EGW00215", "초당 거래건수 초과")

    def test_undocumented_code_falls_back_to_message(self):
        assert is_provider_throttle_reject(
            "EGW99999", "초당 거래건수를 초과하였습니다."
        )

    def test_business_rejections_are_not_throttles(self):
        # The one that must never be re-POSTed: a real order-engine rejection.
        assert not is_provider_throttle_reject(
            "APBK0656", "주문가능금액을 초과하였습니다."
        )
        assert not is_provider_throttle_reject(
            "APBK0918", "주문가능수량을 초과하였습니다"
        )
        assert not is_provider_throttle_reject(None, None)

    def test_backoff_grows_and_is_bounded(self):
        delays = [throttle_backoff_seconds(d) for d in range(MAX_THROTTLE_RESUBMITS)]
        assert delays == sorted(delays)
        assert all(0 < d <= 2.0 for d in delays)

    def test_provider_order_number_makes_gateway_response_ineligible(self):
        assert (
            gateway_throttle_rejection_from_response(
                {
                    **_THROTTLE_BODY,
                    "output": {"ORD_NO": "already-created"},
                },
                http_status=200,
                send_disposition="not_created",
            )
            is None
        )


@pytest.mark.unit
class TestOverseasThrottleCandidate:
    """The low-level BAC/IVV path only returns evidence, never retries."""

    @pytest.mark.asyncio
    async def test_normal_gateway_throttle_is_typed_and_sent_once(self, _no_sleep):
        instance, parent = _overseas([])
        parent._request_with_rate_limit = AsyncMock(
            side_effect=_normal_gateway_throttle
        )

        with pytest.raises(KISGatewayThrottleRejection, match="EGW00201"):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1
        assert _no_sleep == []

    @pytest.mark.asyncio
    async def test_throttle_without_normal_http_evidence_is_terminal_once(
        self, _no_sleep
    ):
        instance, parent = _overseas([_THROTTLE_BODY])

        with pytest.raises(RuntimeError, match="EGW00201"):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1
        assert _no_sleep == []

    @pytest.mark.asyncio
    async def test_candidate_keeps_original_order_parameters_on_the_one_send(self):
        instance, parent = _overseas([])
        parent._request_with_rate_limit = AsyncMock(
            side_effect=_normal_gateway_throttle
        )

        with pytest.raises(KISGatewayThrottleRejection):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        call = parent._request_with_rate_limit.await_args
        assert call.kwargs["json_body"]["PDNO"] == "BAC"
        assert call.kwargs["json_body"]["ORD_QTY"] == "1"
        assert call.kwargs["tr_id"] == "TTTT1006U"

    @pytest.mark.asyncio
    async def test_business_rejection_is_not_reposted(self):
        instance, parent = _overseas(
            [
                {
                    "rt_cd": "1",
                    "msg_cd": "APBK0656",
                    "msg1": "주문가능금액을 초과하였습니다.",
                }
            ]
        )

        with pytest.raises(RuntimeError, match="APBK0656"):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_timeout_still_sends_exactly_once(self):
        instance, parent = _overseas([])
        parent._request_with_rate_limit = AsyncMock(side_effect=httpx.ReadTimeout(""))

        with pytest.raises(httpx.ReadTimeout):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_throttle_body_carried_by_5xx_fails_closed(self):
        """A 5xx never proves the order was not created — no re-POST."""
        instance, parent = _overseas([])

        async def _respond(*args, **kwargs):
            tracker = kwargs["send_outcome"]
            tracker.mark_dispatched()
            tracker.mark_http_response(500)
            return _THROTTLE_BODY

        parent._request_with_rate_limit = AsyncMock(side_effect=_respond)

        with pytest.raises(RuntimeError, match="EGW00201"):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False
            )

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_caller_tracker_is_threaded_to_the_typed_candidate(self):
        instance, parent = _overseas([])
        parent._request_with_rate_limit = AsyncMock(
            side_effect=_normal_gateway_throttle
        )
        tracker = OrderSendOutcomeTracker()

        with pytest.raises(KISGatewayThrottleRejection):
            await instance.order_overseas_stock(
                "BAC", "NYSE", "sell", 1, 62.15, is_mock=False, send_outcome=tracker
            )

        seen = [
            call.kwargs["send_outcome"]
            for call in parent._request_with_rate_limit.await_args_list
        ]
        assert seen == [tracker]


@pytest.mark.unit
class TestDomesticThrottleCandidate:
    @pytest.mark.asyncio
    async def test_normal_gateway_throttle_is_typed_and_sent_once(self):
        instance, parent = _domestic([])
        parent._request_with_rate_limit = AsyncMock(
            side_effect=_normal_gateway_throttle
        )

        with pytest.raises(KISGatewayThrottleRejection, match="EGW00201"):
            await instance.order_korea_stock("005930", "sell", 1, 70000)

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_throttle_without_normal_http_evidence_is_terminal_once(self):
        instance, parent = _domestic([_THROTTLE_BODY])

        with pytest.raises(RuntimeError, match="EGW00201"):
            await instance.order_korea_stock("005930", "sell", 1, 70000)

        assert parent._request_with_rate_limit.await_count == 1

    @pytest.mark.asyncio
    async def test_business_rejection_is_not_reposted(self):
        instance, parent = _domestic(
            [
                {
                    "rt_cd": "1",
                    "msg_cd": "APBK0918",
                    "msg1": "주문가능수량을 초과하였습니다",
                }
            ]
        )

        with pytest.raises(RuntimeError, match="APBK0918"):
            await instance.order_korea_stock("005930", "sell", 1, 70000)

        assert parent._request_with_rate_limit.await_count == 1
